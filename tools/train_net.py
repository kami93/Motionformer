#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.

"""Train a video classification model."""

import numpy as np
import pickle
import pprint
from timm.data import Mixup
import torch
from fvcore.nn.precise_bn import get_bn_modules, update_bn_stats

from slowfast.config.defaults import get_cfg
import slowfast.models.losses as losses
import slowfast.models.optimizer as optim
import slowfast.utils.checkpoint as cu
import slowfast.utils.distributed as du
import slowfast.utils.logging as logging
import slowfast.utils.metrics as metrics
import slowfast.utils.misc as misc
import slowfast.visualization.tensorboard_vis as tb
from slowfast.datasets import loader
from slowfast.models import build_model
from slowfast.utils.meters import TrainMeter, ValMeter, EPICTrainMeter, EPICValMeter
from slowfast.utils.multigrid import MultigridSchedule
# from timm.utils import NativeScaler

logger = logging.get_logger(__name__)


def train_epoch(
    train_loader, model, optimizer, train_meter, cur_epoch, cfg, 
    writer=None, loss_scaler=None, loss_fun=None, mixup_fn=None
):
    """
    Perform the video training for one epoch.
    Args:
        train_loader (loader): video training loader.
        model (model): the video model to train.
        optimizer (optim): the optimizer to perform optimization on the model's
            parameters.
        train_meter (TrainMeter): training meters to log the training performance.
        cur_epoch (int): current epoch of training.
        cfg (CfgNode): configs. Details can be found in
            slowfast/config/defaults.py
        writer (TensorboardWriter, optional): TensorboardWriter object
            to writer Tensorboard log.
    """
    # Enable train mode.
    model.train()

    train_meter.iter_tic()
    data_size = len(train_loader)

    cur_global_batch_size = cfg.NUM_SHARDS * cfg.TRAIN.BATCH_SIZE
    num_iters = cfg.GLOBAL_BATCH_SIZE // cur_global_batch_size
    
    if cur_global_batch_size < cfg.GLOBAL_BATCH_SIZE:
        logger.info("Gradient accumulation enabled!")
        logger.info(f"cur_global_batch_size: {cur_global_batch_size}, target_global_batch_size: {cfg.GLOBAL_BATCH_SIZE}")

    for cur_iter, (inputs, labels, index, meta) in enumerate(train_loader):
        global_step = data_size * cur_epoch + cur_iter + 1

        # Transfer the data to the current GPU device.
        if cfg.NUM_GPUS:
            if isinstance(inputs, (list,)):
                for i in range(len(inputs)):
                    inputs[i] = inputs[i].cuda(non_blocking=True)
            else:
                inputs = inputs.cuda(non_blocking=True)
            for key, val in meta.items():
                if isinstance(val, (list,)):
                    for i in range(len(val)):
                        if not isinstance(val[i], (str,)):
                            val[i] = val[i].cuda(non_blocking=True)
                else:
                    meta[key] = val.cuda(non_blocking=True)

        if mixup_fn is not None:
            labels = labels.cuda()
            inputs, labels = mixup_fn(inputs[0], labels)
            inputs = [inputs]
        else:
            labels = labels.cuda(non_blocking=True)

        # Update the learning rate.
        lr = optim.get_epoch_lr(cur_epoch + float(cur_iter) / data_size, cfg)
        optim.set_lr(optimizer, lr)

        train_meter.data_toc()
        
        # Perform the backward pass.
        if cur_global_batch_size >= cfg.GLOBAL_BATCH_SIZE:
            with torch.cuda.amp.autocast(enabled=cfg.SOLVER.USE_MIXED_PRECISION):
                preds = model(inputs)
                loss = loss_fun(preds, labels)

            if cfg.SOLVER.USE_MIXED_PRECISION:
                optimizer.zero_grad()
                loss_scaler.scale(loss).backward()
                loss_scaler.step(optimizer)
                loss_scaler.update()

            else:
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
        
        else:
            if cur_iter == 0:
                optimizer.zero_grad()
            
            if (cur_iter + 1) % num_iters != 0:
                if cur_iter < num_iters:
                    logger.info(f"{cur_iter + 1}/{data_size}. No Synced forward")
                with model.no_sync():
                    with torch.cuda.amp.autocast(enabled=cfg.SOLVER.USE_MIXED_PRECISION):
                        preds = model(inputs)
                        loss = loss_fun(preds, labels)
                    # no synchronization, accumulate grads
                    if cfg.SOLVER.USE_MIXED_PRECISION:
                        loss_scaler.scale(loss).backward()

                    else:
                        loss.backward()
            
            if (cur_iter + 1) % num_iters == 0:
                if cur_iter < num_iters:
                    logger.info(f"{cur_iter + 1}/{data_size}. Synced forward")
                with torch.cuda.amp.autocast(enabled=cfg.SOLVER.USE_MIXED_PRECISION):
                    preds = model(inputs)
                    loss = loss_fun(preds, labels)
                # synchronize grads
                if cfg.SOLVER.USE_MIXED_PRECISION:
                    loss_scaler.scale(loss).backward()

                else:
                    loss.backward()

                # unscale gradients if mixed precision
                if cfg.SOLVER.USE_MIXED_PRECISION:
                    loss_scaler.unscale_(optimizer)

                # scale gradients so that correct lr@GLOBAL_BATCH_SIZE is applied.
                with torch.no_grad():
                    for name, param in model.named_parameters():
                        if param.grad is None:
                            if cur_iter < num_iters:
                                logger.info(f"Skipping a param {name}")
                        else:
                            if cur_iter < num_iters:
                                logger.info(f"Scaling a param {name} by 1/{num_iters}")
                            param.grad /= num_iters

                if cfg.SOLVER.USE_MIXED_PRECISION:
                    loss_scaler.step(optimizer)
                    loss_scaler.update()
                else:
                    optimizer.step()
                
                optimizer.zero_grad()

        top1_err, top5_err = None, None
            
        num_topks_correct = metrics.topks_correct(preds, labels, (1, 5))
        top1_err, top5_err = [
            (1.0 - x / preds.size(0)) * 100.0 for x in num_topks_correct
        ]

        # Gather all the predictions across all the devices.
        if cfg.NUM_GPUS > 1:
            loss, top1_err, top5_err = du.all_reduce(
                [loss, top1_err, top5_err]
            )

        # Copy the stats from GPU to CPU (sync point).
        loss, top1_err, top5_err = (
            loss.item(),
            top1_err.item(),
            top5_err.item(),
        )

        # Update and log stats.
        train_meter.update_stats(
            top1_err,
            top5_err,
            loss,
            lr,
            inputs[0].size(0)
            * max(
                cfg.NUM_GPUS, 1
            ),
        )
    
        # write to tensorboard format if available.
        if writer is not None:
            writer.add_scalars(
                {
                    "Train/loss": loss,
                    "Train/lr": lr,
                },
                global_step=data_size * cur_epoch + cur_iter,
            )
            writer.add_scalars(
                {
                    "Train/Top1_err": top1_err if top1_err is not None else 0.0,
                    "Train/Top5_err": top5_err if top5_err is not None else 0.0,
                },
                global_step=data_size * cur_epoch + cur_iter,
            )

        train_meter.iter_toc()  # measure allreduce for this meter
        train_meter.log_iter_stats(cur_epoch, cur_iter)
        train_meter.iter_tic()

    # Log epoch stats.
    train_meter.log_epoch_stats(cur_epoch)
    train_meter.reset()

@torch.no_grad()
def eval_epoch(val_loader, model, val_meter, cur_epoch, cfg, writer=None):
    """
    Evaluate the model on the val set.
    Args:
        val_loader (loader): data loader to provide validation data.
        model (model): model to evaluate the performance.
        val_meter (ValMeter): meter instance to record and calculate the metrics.
        cur_epoch (int): number of the current epoch of training.
        cfg (CfgNode): configs. Details can be found in
            slowfast/config/defaults.py
        writer (TensorboardWriter, optional): TensorboardWriter object
            to writer Tensorboard log.
    """

    # Evaluation mode enabled. The running stats would not be updated.
    model.eval()
    val_meter.iter_tic()

    for cur_iter, (inputs, labels, _, meta) in enumerate(val_loader):
        if cfg.NUM_GPUS:
            # Transferthe data to the current GPU device.
            if isinstance(inputs, (list,)):
                for i in range(len(inputs)):
                    inputs[i] = inputs[i].cuda(non_blocking=True)
            else:
                inputs = inputs.cuda(non_blocking=True)
            if isinstance(labels, (dict,)):
                labels = {k: v.cuda() for k, v in labels.items()}
            else:
                labels = labels.cuda()
            for key, val in meta.items():
                if isinstance(val, (list,)):
                    for i in range(len(val)):
                        if not isinstance(val[i], (str,)):
                            val[i] = val[i].cuda(non_blocking=True)
                else:
                    meta[key] = val.cuda(non_blocking=True)
        val_meter.data_toc()

        with torch.cuda.amp.autocast(enabled=cfg.SOLVER.USE_MIXED_PRECISION):
            preds = model(inputs)
            if isinstance(labels, (dict,)) and cfg.TRAIN.DATASET == "Epickitchens":
                # Compute the verb accuracies.
                verb_top1_acc, verb_top5_acc = metrics.topk_accuracies(
                    preds[0], labels['verb'], (1, 5))

                # Combine the errors across the GPUs.
                if cfg.NUM_GPUS > 1:
                    verb_top1_acc, verb_top5_acc = du.all_reduce(
                        [verb_top1_acc, verb_top5_acc])

                # Copy the errors from GPU to CPU (sync point).
                verb_top1_acc, verb_top5_acc = verb_top1_acc.item(), verb_top5_acc.item()

                # Compute the noun accuracies.
                noun_top1_acc, noun_top5_acc = metrics.topk_accuracies(
                    preds[1], labels['noun'], (1, 5))

                # Combine the errors across the GPUs.
                if cfg.NUM_GPUS > 1:
                    noun_top1_acc, noun_top5_acc = du.all_reduce(
                        [noun_top1_acc, noun_top5_acc])

                # Copy the errors from GPU to CPU (sync point).
                noun_top1_acc, noun_top5_acc = noun_top1_acc.item(), noun_top5_acc.item()

                # Compute the action accuracies.
                action_top1_acc, action_top5_acc = metrics.multitask_topk_accuracies(
                    (preds[0], preds[1]),
                    (labels['verb'], labels['noun']),
                    (1, 5))

                # Combine the errors across the GPUs.
                if cfg.NUM_GPUS > 1:
                    action_top1_acc, action_top5_acc = du.all_reduce([action_top1_acc, action_top5_acc])

                # Copy the errors from GPU to CPU (sync point).
                action_top1_acc, action_top5_acc = action_top1_acc.item(), action_top5_acc.item()

                val_meter.iter_toc()
                
                # Update and log stats.
                val_meter.update_stats(
                    (verb_top1_acc, noun_top1_acc, action_top1_acc),
                    (verb_top5_acc, noun_top5_acc, action_top5_acc),
                    inputs[0].size(0) * cfg.NUM_GPUS
                )
                
                # write to tensorboard format if available.
                if writer is not None:
                    writer.add_scalars(
                        {
                            "Val/verb_top1_acc": verb_top1_acc,
                            "Val/verb_top5_acc": verb_top5_acc,
                            "Val/noun_top1_acc": noun_top1_acc,
                            "Val/noun_top5_acc": noun_top5_acc,
                            "Val/action_top1_acc": action_top1_acc,
                            "Val/action_top5_acc": action_top5_acc,
                        },
                        global_step=len(val_loader) * cur_epoch + cur_iter,
                    )
            else:
                # Compute the errors.
                num_topks_correct = metrics.topks_correct(preds, labels, (1, 5))

                # Combine the errors across the GPUs.
                top1_err, top5_err = [
                    (1.0 - x / preds.size(0)) * 100.0 for x in num_topks_correct
                ]
                if cfg.NUM_GPUS > 1:
                    top1_err, top5_err = du.all_reduce([top1_err, top5_err])

                # Copy the errors from GPU to CPU (sync point).
                top1_err, top5_err = top1_err.item(), top5_err.item()

                val_meter.iter_toc()
                # Update and log stats.
                val_meter.update_stats(
                    top1_err,
                    top5_err,
                    inputs[0].size(0)
                    * max(
                        cfg.NUM_GPUS, 1
                    ),
                )
                # write to tensorboard format if available.
                if writer is not None:
                    writer.add_scalars(
                        {"Val/Top1_err": top1_err, "Val/Top5_err": top5_err},
                        global_step=len(val_loader) * cur_epoch + cur_iter,
                    )

            val_meter.update_predictions(preds, labels)

        val_meter.log_iter_stats(cur_epoch, cur_iter)
        val_meter.iter_tic()

    # Log epoch stats.
    val_meter.log_epoch_stats(cur_epoch)
    # write to tensorboard format if available.
    if writer is not None:
        all_preds = [pred.clone().detach() for pred in val_meter.all_preds]
        all_labels = [
            label.clone().detach() for label in val_meter.all_labels
        ]
        if cfg.NUM_GPUS:
            all_preds = [pred.cpu() for pred in all_preds]
            all_labels = [label.cpu() for label in all_labels]
        writer.plot_eval(
            preds=all_preds, labels=all_labels, global_step=cur_epoch
        )

    val_meter.reset()


def calculate_and_update_precise_bn(loader, model, num_iters=200, use_gpu=True):
    """
    Update the stats in bn layers by calculate the precise stats.
    Args:
        loader (loader): data loader to provide training data.
        model (model): model to update the bn stats.
        num_iters (int): number of iterations to compute and update the bn stats.
        use_gpu (bool): whether to use GPU or not.
    """

    def _gen_loader():
        for inputs, *_ in loader:
            if use_gpu:
                if isinstance(inputs, (list,)):
                    for i in range(len(inputs)):
                        inputs[i] = inputs[i].cuda(non_blocking=True)
                else:
                    inputs = inputs.cuda(non_blocking=True)
            yield inputs

    # Update the bn stats.
    update_bn_stats(model, _gen_loader(), num_iters)


def build_trainer(cfg):
    """
    Build training model and its associated tools, including optimizer,
    dataloaders and meters.
    Args:
        cfg (CfgNode): configs. Details can be found in
            slowfast/config/defaults.py
    Returns:
        model (nn.Module): training model.
        optimizer (Optimizer): optimizer.
        train_loader (DataLoader): training data loader.
        val_loader (DataLoader): validatoin data loader.
        precise_bn_loader (DataLoader): training data loader for computing
            precise BN.
        train_meter (TrainMeter): tool for measuring training stats.
        val_meter (ValMeter): tool for measuring validation stats.
    """
    # Build the video model and print model statistics.
    model = build_model(cfg)
    if du.is_master_proc() and cfg.LOG_MODEL_INFO and cfg.DATA.INPUT_TYPE == 'rgb':
        misc.log_model_info(model, cfg, use_train_input=True)

    # Construct the optimizer.
    optimizer = optim.construct_optimizer(model, cfg)

    # Create the video train and val loaders.
    train_loader = loader.construct_loader(cfg, "train")
    val_loader = loader.construct_loader(cfg, "val")
    precise_bn_loader = loader.construct_loader(
        cfg, "train", is_precise_bn=True
    )
    # Create meters.
    train_meter = TrainMeter(len(train_loader), cfg)
    val_meter = ValMeter(len(val_loader), cfg)

    return (
        model,
        optimizer,
        train_loader,
        val_loader,
        precise_bn_loader,
        train_meter,
        val_meter,
    )


def train(cfg):
    """
    Train a video model for many epochs on train set and evaluate it on val set.
    Args:
        cfg (CfgNode): configs. Details can be found in
            slowfast/config/defaults.py
    """
    # Set up environment.
    du.init_distributed_training(cfg)
    # Set random seed from configs.
    np.random.seed(cfg.RNG_SEED)
    torch.manual_seed(cfg.RNG_SEED)

    # Setup logging format.
    logging.setup_logging(cfg.OUTPUT_DIR)

    # Init multigrid.
    multigrid = None
    if cfg.MULTIGRID.LONG_CYCLE or cfg.MULTIGRID.SHORT_CYCLE:
        multigrid = MultigridSchedule()
        cfg = multigrid.init_multigrid(cfg)
        if cfg.MULTIGRID.LONG_CYCLE:
            cfg, _ = multigrid.update_long_cycle(cfg, cur_epoch=0)
    
    # Print config.
    logger.info("Train with config:")
    logger.info(pprint.pformat(cfg))

    # Build the video model and print model statistics.
    model = build_model(cfg)
    if du.is_master_proc() and cfg.LOG_MODEL_INFO:
        misc.log_model_info(model, cfg, use_train_input=True)

    # Construct the optimizer.
    optimizer = optim.construct_optimizer(model, cfg)

    # Mixed Precision Training Scaler
    if cfg.SOLVER.USE_MIXED_PRECISION:
        loss_scaler = torch.cuda.amp.GradScaler()
    else:
        loss_scaler = None

    # Load a checkpoint to resume training if applicable.
    start_epoch = cu.load_train_checkpoint(
        cfg, model, optimizer, loss_scaler=loss_scaler)

    # Create the video train and val loaders.
    train_loader = loader.construct_loader(cfg, "train")
    val_loader = loader.construct_loader(cfg, "val")
    precise_bn_loader = (
        loader.construct_loader(cfg, "train", is_precise_bn=True)
        if cfg.BN.USE_PRECISE_STATS
        else None
    )

    # Create meters.
    if cfg.TRAIN.DATASET == 'Epickitchens':
        train_meter = EPICTrainMeter(len(train_loader), cfg)
        val_meter = EPICValMeter(len(val_loader), cfg)
    else:
        train_meter = TrainMeter(len(train_loader), cfg)
        val_meter = ValMeter(len(val_loader), cfg)

    # set up writer for logging to Tensorboard format.
    if cfg.TENSORBOARD.ENABLE and du.is_master_proc(
        cfg.NUM_GPUS * cfg.NUM_SHARDS
    ):
        writer = tb.TensorboardWriter(cfg)
    else:
        writer = None

    # Perform the training loop.
    logger.info("Start epoch: {}".format(start_epoch + 1))
    
    mixup_fn = None
    mixup_active = cfg.MIXUP.MIXUP_ALPHA > 0 or cfg.MIXUP.CUTMIX_ALPHA > 0 or cfg.MIXUP.CUTMIX_MINMAX is not None
    if mixup_active:
        mixup_fn = Mixup(
            mixup_alpha=cfg.MIXUP.MIXUP_ALPHA, 
            cutmix_alpha=cfg.MIXUP.CUTMIX_ALPHA, 
            cutmix_minmax=cfg.MIXUP.CUTMIX_MINMAX,
            prob=cfg.MIXUP.MIXUP_PROB, 
            switch_prob=cfg.MIXUP.MIXUP_SWITCH_PROB, 
            mode=cfg.MIXUP.MIXUP_MODE,
            label_smoothing=cfg.SOLVER.SMOOTHING, 
            num_classes=cfg.MODEL.NUM_CLASSES
        )

    # Explicitly declare reduction to mean.
    if cfg.MIXUP.MIXUP_ALPHA > 0.:
        # smoothing is handled with mixup label transform
        loss_fun = losses.get_loss_func("soft_target_cross_entropy")()
    elif cfg.SOLVER.SMOOTHING > 0.0:
        loss_fun = losses.get_loss_func("label_smoothing_cross_entropy")(
            smoothing=cfg.SOLVER.SMOOTHING)
    else:
        loss_fun = losses.get_loss_func(cfg.MODEL.LOSS_FUNC)(reduction="mean")

    for cur_epoch in range(start_epoch, cfg.SOLVER.MAX_EPOCH):
        if cfg.MULTIGRID.LONG_CYCLE:
            cfg, changed = multigrid.update_long_cycle(cfg, cur_epoch)
            if changed:
                (
                    model,
                    optimizer,
                    train_loader,
                    val_loader,
                    precise_bn_loader,
                    train_meter,
                    val_meter,
                ) = build_trainer(cfg)

                # Load checkpoint.
                if cu.has_checkpoint(cfg.OUTPUT_DIR):
                    last_checkpoint = cu.get_last_checkpoint(cfg.OUTPUT_DIR)
                    assert "{:05d}.pyth".format(cur_epoch) in last_checkpoint
                else:
                    last_checkpoint = cfg.TRAIN.CHECKPOINT_FILE_PATH
                logger.info("Load from {}".format(last_checkpoint))
                cu.load_checkpoint(
                    last_checkpoint, model, cfg.NUM_GPUS > 1, optimizer
                )

        # Shuffle the dataset.
        loader.shuffle_dataset(train_loader, cur_epoch)

        # Train for one epoch.
        train_epoch(
            train_loader, model, optimizer, train_meter, cur_epoch, cfg, writer, 
            loss_scaler=loss_scaler, loss_fun=loss_fun, mixup_fn=mixup_fn)

        is_checkp_epoch = cu.is_checkpoint_epoch(
            cfg,
            cur_epoch,
            None if multigrid is None else multigrid.schedule,
        )
        is_eval_epoch = misc.is_eval_epoch(
            cfg, cur_epoch, None if multigrid is None else multigrid.schedule
        )

        # Compute precise BN stats.
        if (
            (is_checkp_epoch or is_eval_epoch)
            and cfg.BN.USE_PRECISE_STATS
            and len(get_bn_modules(model)) > 0
        ):
            calculate_and_update_precise_bn(
                precise_bn_loader,
                model,
                min(cfg.BN.NUM_BATCHES_PRECISE, len(precise_bn_loader)),
                cfg.NUM_GPUS > 0,
            )
        _ = misc.aggregate_sub_bn_stats(model)

        # Save a checkpoint.
        if is_checkp_epoch:
            cu.save_checkpoint(cfg.OUTPUT_DIR, model, optimizer, cur_epoch, cfg, 
                loss_scaler=loss_scaler)
        # Evaluate the model on validation set.
        if is_eval_epoch:
            eval_epoch(val_loader, model, val_meter, cur_epoch, cfg, writer)

    if writer is not None:
        writer.close()

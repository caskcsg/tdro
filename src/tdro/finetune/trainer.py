#!/usr/bin/python
# -*- encoding: utf-8 -*-
'''
Trainer for contrastive loss.

@Time    :   2023/11/06
@Author  :   Ma (Ma787639046@outlook.com)
'''

import os
import json
import datetime
from copy import deepcopy
from collections import defaultdict
from typing import Dict, List, Tuple, Optional, Any, Union

import torch
from torch import nn
import torch.distributed as dist
import transformers
from transformers import (
    BatchEncoding,
    get_scheduler,
    __version__
)
from transformers.trainer import Trainer
from transformers.trainer_utils import PREFIX_CHECKPOINT_DIR
from transformers.modeling_utils import unwrap_model
from transformers.configuration_utils import PretrainedConfig
from transformers.utils.import_utils import is_sagemaker_mp_enabled
from transformers.utils import WEIGHTS_NAME, WEIGHTS_INDEX_NAME, CONFIG_NAME

from ..scheduler import get_linear_schedule_with_warmup_minlr, get_cosine_schedule_with_warmup_minlr

import logging
logger = logging.getLogger(__name__)

try:
    from grad_cache import GradCache
    _grad_cache_available = True
except ModuleNotFoundError:
    _grad_cache_available = False

def rewrite_logs(d):
    # Here we monkey patch the rewriter for Wandb/TFboard/etc.
    # to make logging of α/loss_per_group/channel/... indivisual groups
    new_d = {}
    eval_prefix = "eval_"
    eval_prefix_len = len(eval_prefix)
    test_prefix = "test_"
    test_prefix_len = len(test_prefix)
    for k, v in d.items():
        if k.startswith(eval_prefix):
            new_d["eval/" + k[eval_prefix_len:]] = v
        elif k.startswith(test_prefix):
            new_d["test/" + k[test_prefix_len:]] = v
        elif any(_name in k for _name in [
            "α", "loss_per_group", "channel"
        ]):
            new_d[k] = v
        else:
            new_d["train/" + k] = v
    return new_d

transformers.integrations.integration_utils.rewrite_logs = rewrite_logs

class ContrastiveTrainer(Trainer):
    """
    Huggingface Trainer for DPR
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if hasattr(self.args, "negatives_x_device") and self.args.negatives_x_device:
            if not dist.is_initialized():
                raise ValueError('Distributed training has not been initialized for representation all gather.')
            self._dist_loss_scale_factor = dist.get_world_size()
        else:
            self._dist_loss_scale_factor = 1
        # Inject Customised logging behavior
        self.customized_logging_list = defaultdict(list)
        # Redirect loss logs to local file
        if self.args.local_rank <= 0:   # For local_rank == 0
            if hasattr(self.args, 'logging_path') and self.args.logging_path is not None and os.path.exists(self.args.logging_path) and os.path.isfile(self.args.logging_path):
                self.log_file = open(self.args.logging_path, 'a+')
    
    def prediction_step(
        self,
        model: nn.Module,
        inputs: Dict[str, Union[torch.Tensor, Any]],
        prediction_loss_only: bool,
        ignore_keys: Optional[List[str]] = None,
    ) -> Tuple[Optional[torch.Tensor], Optional[torch.Tensor], Optional[torch.Tensor]]:
        """ 
        Contrative Learning does not produce labels at dataloader.
        Here we add all zero labels for `eval_step`.
        """
        (loss, logits, labels) = super().prediction_step(model=model, inputs=inputs, prediction_loss_only=prediction_loss_only, ignore_keys=ignore_keys)

        if labels is None:
            labels = torch.zeros(logits.shape[0], device=logits.device, dtype=torch.int)
        
        return (loss, logits, labels)

    def _dist_gather_tensor(self, t: Optional[torch.Tensor]):
        if t is None:
            return None
        t = t.contiguous()

        all_tensors = [torch.empty_like(t) for _ in range(self.args.world_size)]
        dist.all_gather(all_tensors, t)

        all_tensors[self.args.process_index] = t
        all_tensors = torch.cat(all_tensors, dim=0)

        return all_tensors

    def compute_loss(self, model, inputs, return_outputs=False):
        """
        A neat compute_loss that supports customized logging

        """
        outputs = model(**inputs)
        # We don't use .loss here since the model may return tuples instead of ModelOutput.
        loss = outputs["loss"] if isinstance(outputs, dict) else outputs[0]

        # Inject Customised logging behavior, only support Dict outputs
        logs: dict = outputs.get("logs", None) if isinstance(outputs, dict) else None
        self._log_custom(logs)

        return (loss, outputs) if return_outputs else loss
    
    def training_step(self, *args, **kwargs):
        # Scale back the loss after backwards, to correct log the actual loss scale.
        return super().training_step(*args, **kwargs) / self._dist_loss_scale_factor
    
    def _log_custom(self, logs: Dict[str, float]):
        """ Record Logs from Model """
        if not logs:
            return
        
        if not isinstance(logs, dict):
            raise TypeError("Only Dict is accepted for customized logging.")

        for k, v in logs.items():
            # Set maxlen of list to avoid memory leak, useful when
            # customized_logging_list has not been cleaned correctly
            if len(self.customized_logging_list[k]) < 5000: 
                self.customized_logging_list[k].append(v)
    
    def _save(self, output_dir: Optional[str] = None, state_dict=None):
        # If we are executing this function, we are the process zero, so we don't check for that.
        output_dir = output_dir if output_dir is not None else self.args.output_dir
        os.makedirs(output_dir, exist_ok=True)
        logger.info("Saving model checkpoint to %s", output_dir)

        if state_dict is None:
            state_dict = self.accelerator.get_state_dict(self.model)

        model = unwrap_model(self.model)
        if hasattr(model, 'save'):     # Encoder.save()
            model.save(output_dir, state_dict=state_dict, safe_serialization=self.args.save_safetensors)
        elif hasattr(model, 'save_pretrained'):
            model.save_pretrained(output_dir, state_dict=state_dict, safe_serialization=self.args.save_safetensors)
        else:
            raise NotImplementedError(
                f'MODEL {self.model.__class__.__name__} '
                f'does not support save_pretrained interface')
    
        if self.tokenizer is not None:
            self.tokenizer.save_pretrained(output_dir)

        # Good practice: save your training arguments together with the trained model
        torch.save(self.args, os.path.join(output_dir, "training_args.bin"))
    
    def _save_checkpoint(self, model, trial, metrics=None):
        super()._save_checkpoint(model=model, trial=trial, metrics=metrics)

        # FSDP Saving Reference: https://huggingface.co/docs/accelerate/main/en/usage_guides/fsdp#state-dict
        if self.is_fsdp_enabled:
            checkpoint_folder = f"{PREFIX_CHECKPOINT_DIR}-{self.state.global_step}"
            run_dir = self._get_output_dir(trial=trial)
            output_dir = os.path.join(run_dir, checkpoint_folder)

            state_dict = self.accelerator.get_state_dict(model)
            if self.accelerator.is_main_process:
                self._save(output_dir, state_dict=state_dict)
            
            dist.barrier()

    # Compatible with Transformers>=4.24.0
    def _load_from_checkpoint(self, resume_from_checkpoint: str, model: nn.Module=None):
        if self.args.deepspeed or self.is_fsdp_enabled:
            return super()._load_from_checkpoint(resume_from_checkpoint, model=model)

        if is_sagemaker_mp_enabled():
            raise NotImplementedError()

        if model is None:
            model = self.model

        logger.info(f"Loading model from {resume_from_checkpoint}.")

        # DP, DDP
        if hasattr(model, "load"):
            # Load Pre-training checkpoint with header from local files
            load_kwargs = dict()
            if hasattr(model, "model_args"):
                load_kwargs["model_args"] = deepcopy(model.model_args)
            if hasattr(model, "train_args"):
                load_kwargs["train_args"] = deepcopy(model.train_args)
            if hasattr(model, "data_args"):
                load_kwargs["data_args"] = deepcopy(model.data_args)
            
            model_loaded: nn.Module = model.load(resume_from_checkpoint, **load_kwargs)
            load_results = self.model.load_state_dict(model_loaded.state_dict())
        else:
            # Load all other models to `model`
            super()._load_from_checkpoint(resume_from_checkpoint, model=model)
    
    def _load_best_model(self):
        logger.info(f"Loading best model from {self.state.best_model_checkpoint} (score: {self.state.best_metric}).")
        self._load_from_checkpoint(self.state.best_model_checkpoint)
    
    def create_scheduler(self, num_training_steps: int, optimizer: torch.optim.Optimizer = None):
        """
        Setup the scheduler. The optimizer of the trainer must have been set up either before this method is called or
        passed as an argument.

        Args:
            num_training_steps (int): The number of training steps to do.
        """
        if self.lr_scheduler is None:
            if self.args.lr_scheduler_type == "linear":
                self.lr_scheduler = get_linear_schedule_with_warmup_minlr(
                    optimizer=self.optimizer if optimizer is None else optimizer,
                    num_warmup_steps=self.args.get_warmup_steps(num_training_steps),
                    num_training_steps=num_training_steps,
                    min_lr_ratio=self.args.min_lr_ratio,
                )
            elif self.args.lr_scheduler_type == "cosine":
                self.lr_scheduler = get_cosine_schedule_with_warmup_minlr(
                    optimizer=self.optimizer if optimizer is None else optimizer,
                    num_warmup_steps=self.args.get_warmup_steps(num_training_steps),
                    num_training_steps=num_training_steps,
                    min_lr_ratio=self.args.min_lr_ratio,
                )
            else:
                self.lr_scheduler = get_scheduler(
                    self.args.lr_scheduler_type,
                    optimizer=self.optimizer if optimizer is None else optimizer,
                    num_warmup_steps=self.args.get_warmup_steps(num_training_steps),
                    num_training_steps=num_training_steps,
                )
            self._created_lr_scheduler = True
        return self.lr_scheduler

    def log(self, logs: Dict[str, float]) -> None:
        """
        Log `logs` on the various objects watching training.

        Subclass and override this method to inject custom behavior.

        Args:
            logs (`Dict[str, float]`):
                The values to log.
        """
        if self.state.epoch is not None:
            logs["epoch"] = round(self.state.epoch, 2)
        
        # Inject Customised logging behavior
        for k, v in self.customized_logging_list.items():
            if len(v) > 0:
                logs[k] = sum(v) / len(v)
                if "lr" not in k:
                    logs[k] = round(logs[k], 6)
        self.customized_logging_list.clear()

        output = {**logs, **{"step": self.state.global_step}}
        self.state.log_history.append(output)
        self.control = self.callback_handler.on_log(self.args, self.state, self.control, logs)

        # Save log to file
        if self.args.local_rank <= 0 and hasattr(self, 'log_file'):
            self.log_file.write(f'{datetime.datetime.now()} - {json.dumps(output)}\n')
            self.log_file.flush()


class GCTrainer(ContrastiveTrainer):
    def __init__(self, *args, **kwargs):
        logger.info('Initializing Gradient Cache Trainer')
        if not _grad_cache_available:
            raise ValueError(
                'Grad Cache package not available. You can obtain it from https://github.com/luyug/GradCache.')
        super(GCTrainer, self).__init__(*args, **kwargs)
        
        assert self.accelerator is not None
        self.gc = GradCache(
            models=[self.model, self.model],
            chunk_sizes=[self.args.gc_q_chunk_size, self.args.gc_p_chunk_size],
            loss_fn=self.gc_loss_fn,
            compute_loss_context_manager=self.compute_loss_context_manager,
            accelerator=self.accelerator,
            get_rep_fn=lambda x: x.p_reps if x.p_reps is not None else x.q_reps,
        )

        if self.args.no_sync_except_last and (not self.args._no_sync_in_gradient_accumulation):
            logger.warning(f"No sync for gradient accumulation is not compable with Deepspeed. Setting `no_sync_except_last` to False.")
            self.args.no_sync_except_last = False
    
    def gc_loss_fn(self, *reps, **loss_kwargs):
        outputs = self.model.compute_loss(*reps, **loss_kwargs)
        self._log_custom(outputs.logs)   # Inject Customised logging behavior
        return outputs.loss

    def training_step(self, model: nn.Module, inputs: Dict[str, any]) -> torch.Tensor:
        model.train()
        inputs = self._prepare_inputs(inputs)
        queries, passages = {'query': inputs.pop('query')}, {'passage': inputs.pop('passage')}

        self.gc.models = [model, model]
        loss = self.gc(queries, passages, no_sync_except_last=self.args.no_sync_except_last, **inputs)

        return loss / self.args.gradient_accumulation_steps / self._dist_loss_scale_factor

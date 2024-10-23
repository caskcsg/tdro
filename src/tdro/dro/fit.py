#!/usr/bin/python
# -*- encoding: utf-8 -*-
'''
Training scripts.

@Time    :   2023/12/04
@Author  :   Ma (Ma787639046@outlook.com)
'''

import os
import sys

# Hacking with Fused Ops
from ..utils.monkey_patch import hacking_fused_rms_norm
hacking_fused_rms_norm()

import torch
import transformers
from transformers import (
    HfArgumentParser,
    set_seed,
)
from transformers.trainer_utils import get_last_checkpoint

from datasets.distributed import split_dataset_by_node

from ..utils.data_utils import construct_domain_dataset, load_tokenizer
from ..finetune.data_utils import TrainDataset, TrainCollator, IterableTrainCollator
from ..finetune.metrics import compute_metrics_warpper
from .trainer import DROTrainer as Trainer, DROGCTrainer as GCTrainer

from .arguments import (
    DistributationRobustOptimizationModelArguments as ModelArguments,
    DataArguments,
    DistributationRobustOptimizationRetrieverTrainingArguments as TrainingArguments
)
from .modeling_group_dro_v2 import DROModelv2

import logging
logger = logging.getLogger(__name__)

_MODEL_CLS = {
    "DROModelv2": DROModelv2,
}

def main():
    # See all possible arguments in src/transformers/training_args.py
    # or by passing the --help flag to this script.
    # We now keep distinct sets of args, for a cleaner separation of concerns.

    def _parse_args() -> tuple[ModelArguments, DataArguments, TrainingArguments]:
        # Wrap args parsing in this function to support type hint
        parser = HfArgumentParser((ModelArguments, DataArguments, TrainingArguments))
        if len(sys.argv) == 2 and sys.argv[1].endswith(".json"):
            # If we pass only one argument to the script and it's the path to a json file,
            # let's parse it to get our arguments.
            return parser.parse_json_file(json_file=os.path.abspath(sys.argv[1]))
        else:
            return parser.parse_args_into_dataclasses()

    model_args, data_args, training_args = _parse_args()

    # Setup logging
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    if training_args.should_log:
        # The default of training_args.log_level is passive, so we set log level at info here to have that default.
        transformers.utils.logging.set_verbosity_info()

    log_level = training_args.get_process_log_level()
    logger.setLevel(log_level)
    transformers.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.enable_default_handler()
    transformers.utils.logging.enable_explicit_format()

    # Log on each process the small summary:
    logger.warning(
        f"Process rank: {training_args.local_rank}, device: {training_args.device}, n_gpu: {training_args.n_gpu}, "
        + f"distributed training: {bool(training_args.local_rank != -1)}, fp16: {training_args.fp16}, bf16: {training_args.bf16}"
    )
    logger.info(f"Training/evaluation parameters {training_args}")
    logger.info(f"Model parameters {model_args}")
    logger.info(f"Data parameters {data_args}")
    
    if training_args.logging_path:
        _log_file_folder = os.path.split(training_args.logging_path)
        if len(_log_file_folder) == 2 and _log_file_folder[0] != "":
            os.makedirs(_log_file_folder[0], exist_ok=True)
        log_file_handler = logging.FileHandler(training_args.logging_path)
        transformers.utils.logging.add_handler(log_file_handler)

    # Detecting last checkpoint.
    last_checkpoint = None
    if os.path.isdir(training_args.output_dir) and training_args.do_train and not training_args.overwrite_output_dir:
        last_checkpoint = get_last_checkpoint(training_args.output_dir)
        if last_checkpoint is None and len([i for i in os.listdir(training_args.output_dir) if i != "runs"]) > 0:
            raise ValueError(
                f"Output directory ({training_args.output_dir}) already exists and is not empty. "
                "Use --overwrite_output_dir to overcome."
            )
        elif last_checkpoint is not None and training_args.resume_from_checkpoint is None:
            logger.info(
                f"Checkpoint detected, resuming training at {last_checkpoint}. To avoid this behavior, change "
                "the `--output_dir` or add `--overwrite_output_dir` to train from scratch."
            )

    # Set seed before initializing model.
    set_seed(training_args.seed)

    if training_args.gradient_checkpointing:
        training_args.gradient_checkpointing_kwargs = {"use_reentrant": False}

    tokenizer = load_tokenizer(
        model_name_or_path=model_args.model_name_or_path,
        tokenizer_name=model_args.tokenizer_name,
        cache_dir=model_args.cache_dir,
        use_fast=model_args.use_fast_tokenizer,
    )

    if data_args.domain_config_path is not None:
        # Construct domain datasets
        train_set, domain_config = construct_domain_dataset(
            domain_config_path=data_args.domain_config_path,
            preprocessed_dir=data_args.preprocessed_dir,
            add_domain_id=data_args.add_domain_id,
            add_domain_name=data_args.add_domain_name,
            seed=training_args.seed,
            shuffle=True,
            homogenous_batch=data_args.homogenous_batch,
            global_batch_size=training_args.per_device_train_batch_size * training_args.world_size,
        )

        if not data_args.add_domain_name:
            logger.warning("`add_domain_name` is disabled, training with clusterining or classification tasks will not disable their coresponding in-batch / cross-batch negatives. If you are training on these tasks, please set this argument to `True`.")

        # Trainer will use IterableDatasetShard to wrap the IterableDataset
        # Thus there is no need to use both IterableDatasetShard and split_dataset_by_node !!!
        # train_set = split_dataset_by_node(train_set, training_args.process_index, training_args.world_size) # Split by node

        training_args.accelerator_config.dispatch_batches = False   # Use `DataLoaderShard`

        # Add n_groups to model_args
        model_args.epoch = domain_config['epoch']
        model_args.domain_weights = domain_config['domain_weights']
        model_args.n_groups = len(domain_config['domain_weights'])
        model_args.domain_to_idx = domain_config['domain_ids']
        model_args.domain_size = domain_config['size']

        dev_set = None

        if (not data_args.add_prompt) and (data_args.add_prompt_prob > 0):
            logger.warning(f"Setting add_prompt_prob to -1. If you want to finetune with query prompt, please enable `--add_prompt`.")
            data_args.add_prompt_prob = -1.0

        # Data collator    
        data_collator = IterableTrainCollator(
            tokenizer=tokenizer,
            padding='max_length' if data_args.pad_to_max_length else 'longest',
            pad_to_multiple_of=data_args.pad_to_multiple_of,
            q_max_len=data_args.q_max_len,
            p_max_len=data_args.p_max_len,
            train_n_passages=data_args.train_n_passages, 
            seed=training_args.seed, 
            positive_passage_no_shuffle=data_args.positive_passage_no_shuffle,
            negative_passage_no_shuffle=data_args.negative_passage_no_shuffle,
            add_prompt_prob=data_args.add_prompt_prob,
            prompt_type=data_args.prompt_type,
        )
    else:
        # Json format dataset (Single dataset)
        train_set = TrainDataset(
            data_args=data_args, 
            dataset=data_args.corpus_path,
            query_collection=data_args.query_collection,
            passage_collection=data_args.passage_collection,
            train_n_passages=data_args.train_n_passages,
            positive_passage_no_shuffle=data_args.positive_passage_no_shuffle,
            negative_passage_no_shuffle=data_args.negative_passage_no_shuffle,
        )

        dev_set = None
        if data_args.dev_path is not None:
            dev_set = TrainDataset(
                data_args=data_args, 
                dataset=data_args.dev_path,
                train_n_passages=data_args.eval_n_passages,
                positive_passage_no_shuffle=data_args.positive_passage_no_shuffle,
                negative_passage_no_shuffle=True,
                # Note:
                # We encourage you to enable `--save_text` when making dev sets
                # because sometimes we want to use dev sets from another domain
                # for development set.

                # query_collection=data_args.query_collection,
                # passage_collection=data_args.passage_collection,
            )
        
        # Data collator    
        data_collator = TrainCollator(
            tokenizer=tokenizer,
            padding='max_length' if data_args.pad_to_max_length else 'longest',
            pad_to_multiple_of=data_args.pad_to_multiple_of,
            q_max_len=data_args.q_max_len,
            p_max_len=data_args.p_max_len,
        )

    # Model
    model = _MODEL_CLS[model_args.dro_type].build(
        model_args=model_args, 
        train_args=training_args,
        data_args=data_args,

        # HF Argument
        attn_implementation=training_args.attn_implementation,
        
        # # Do NOT pass a dtype, in order to init model in its own precision for mix-precision training
        # # Ref: https://github.com/huggingface/accelerate/issues/2624#issuecomment-2041406696
        # torch_dtype=torch.bfloat16 if training_args.bf16 else (torch.float16 if training_args.fp16 else None),
    )
    logger.info(model)  # Print Model

    # Initialize our Trainer
    trainer_cls = GCTrainer if training_args.grad_cache or training_args.grad_cache_ac else Trainer
    trainer = trainer_cls(
        model=model,
        args=training_args,
        train_dataset=train_set if training_args.do_train else None,
        eval_dataset=dev_set if training_args.do_eval else None,
        tokenizer=tokenizer,
        data_collator=data_collator,
        compute_metrics=compute_metrics_warpper(metric_for_best_model=training_args.metric_for_best_model) if training_args.do_eval else None,
    )
    train_set.trainer = trainer
    if dev_set is not None:
        dev_set.trainer = trainer
    
    if training_args.deepspeed_plugin is not None and training_args.gradient_checkpointing:
        training_args.gradient_checkpointing_kwargs["ds_config"] = training_args.deepspeed_plugin.deepspeed_config

    # Training
    if training_args.do_train:
        checkpoint = None
        if training_args.resume_from_checkpoint is not None:
            checkpoint = training_args.resume_from_checkpoint
        elif last_checkpoint is not None:
            checkpoint = last_checkpoint
        train_result = trainer.train(resume_from_checkpoint=checkpoint)
        trainer.save_model()  # Saves the tokenizer too for easy upload

        if trainer.is_fsdp_enabled:
            state_dict = trainer.accelerator.get_state_dict(trainer.model)
            if trainer.accelerator.is_main_process:
                trainer._save(training_args.output_dir, state_dict=state_dict)
    
    if training_args.do_eval:
        eval_results = trainer.evaluate()
        logger.info(f"Eval results: {eval_results}")


def _mp_fn(index):
    # For xla_spawn (TPUs)
    main()


if __name__ == "__main__":
    main()

import os
import json
import time
import pprint
import warnings
import pickle
import argparse
from pathlib import Path
from functools import partial

import torch
import torch.nn as nn
from torch.utils.data import DataLoader

from ray.tune.schedulers import ASHAScheduler
from ray.tune import CLIReporter
from ray import tune as hyperparam_tune
from ray import tune
from ray.tune import CLIReporter
from ray.tune.schedulers import ASHAScheduler


import sys
sys.path.append("c:/Users/spran/OneDrive - Indian Institute of Technology (BHU), Varanasi/Desktop/BTP/TueSan/")
sys.path.append("c:/Users/spran/OneDrive - Indian Institute of Technology (BHU), Varanasi/Desktop/BTP/TueSan/Task2/RuleClassification")
sys.path.append("c:/Users/spran/OneDrive - Indian Institute of Technology (BHU), Varanasi/Desktop/BTP/TueSan/Task2/Seq2Seq_Decoding")

from Task2.RuleClassification.logger import logger
from training import train
from Task2.RuleClassification.scoring import evaluate
from Task2.RuleClassification.stemming_rules import evaluate_coverage
from Task2.RuleClassification.generate_dataset import construct_train_dataset
from Task2.RuleClassification.helpers import load_data, save_task2_predictions
from model import build_model, build_optimizer, save_model
from Task2.RuleClassification.uni2intern import internal_transliteration_to_unicode as to_uni
from Task2.RuleClassification.index_dataset import index_dataset, train_collate_fn, eval_collate_fn
from Task2.RuleClassification.config import train_config, tune_config
from Task2.RuleClassification.evaluate import (
    evaluate_model,
    print_metrics,
    format_predictions,
    convert_eval_if_translit,
)
from Task2.RuleClassification.helpers import load_task2_test_data

# Ignore warning (who cares?)

warnings.filterwarnings("ignore")
pp = pprint.PrettyPrinter(indent=4)


def pred_eval(
    model, eval_data, eval_dataloader, indexer, device, tag_rules, translit=False
):

    eval_predictions = evaluate_model(
        model, eval_dataloader, indexer, device, tag_rules, translit
    )

    # Evaluate
    if translit:
        eval_data = convert_eval_if_translit(eval_data)
    
    scores = evaluate([dp[1] for dp in eval_data], eval_predictions, task_id="t2")
    return scores


def train_model(config, checkpoint_dir=None):
    data_path = os.path.join(config["cwd"], "temp_train_data_task2.pickle")
    with open(data_path, "rb") as tf:
        data = pickle.load(tf)
        indexed_train_data, indexed_eval_data, eval_data, stem_rules, tags, tag_rules, indexer = data

    # Build dataloaders
    batch_size = config["batch_size"]
    train_dataloader = DataLoader(
        indexed_train_data,
        batch_size=batch_size,
        collate_fn=train_collate_fn,
        shuffle=True,
    )
    
    eval_dataloader = DataLoader(
        indexed_eval_data,
        batch_size=batch_size,
        collate_fn=eval_collate_fn,
        shuffle=False,
    )

    # Build model
    model = build_model(config, indexer, tag_rules)
    use_cuda = config["cuda"]
    use_cuda = use_cuda and torch.cuda.is_available()
    device = torch.device("cuda" if use_cuda else "cpu")
    model = model.to(device)

    # Build optimizer
    optimizer = build_optimizer(model, config)
    
    if checkpoint_dir is not None:
        checkpoint_path = Path(checkpoint_dir, "checkpoint")
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        model_state, optimizer_state = torch.load(checkpoint_path)
        model.load_state_dict(model_state)
        optimizer.load_state_dict(optimizer_state)

    # Train
    epochs = config["epochs"]
    logger.info(f"Training for {epochs} epochs\n")
    
    evaluator = partial(
        pred_eval,
        eval_data=eval_data,
        eval_dataloader=eval_dataloader,
        indexer=indexer,
        device=device,
        tag_rules = tag_rules,
        translit=config["translit"],
    )
    tune = config["tune"]
    
    if not tune:
        logger.info(f"Training for {config['epochs']} epochs")

    res = train(
        model,
        optimizer,
        train_dataloader,
        config["epochs"],
        device,
        tag_rules,
        config["lr"],
        evaluator,
        tune=tune,
        verbose=not config["tune"]
)
    
    if not tune:
        return res


def main(tune, num_samples=10, max_num_epochs=20, gpus_per_trial=1):
    logger.info(f"Tune: {tune}")
    config = tune_config if tune else train_config
    config["tune"] = tune

    # Dynamically set the current working directory to the project root
    project_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../..")
    )
    config["cwd"] = project_root

    # Update paths to point to the `sanskrit` folder
    config["train_path"] = os.path.join(project_root, "sanskrit", "wsmp_train.json")
    config["eval_path"] = os.path.join(project_root, "sanskrit", "wsmp_dev.json")
    config["test_path"] = os.path.join(project_root, "sanskrit")

    # Load data
    logger.info("Load data")
    translit = config["translit"]
    test = config["test"]

    if translit:
        logger.info("Transliterating input")
    else:
        logger.info("Using raw input")

    train_data = load_data(config["train_path"], translit)
    eval_data = load_data(config["eval_path"], translit)
    test_data = load_task2_test_data(
        Path(config["test_path"], "task_2_input_sentences.tsv"), translit
    )

    logger.info(f"Loaded {len(train_data)} train sents")
    logger.info(f"Loaded {len(eval_data)} test sents")
    logger.info(f"Loaded {len(test_data)} test sents")

    # Generate datasets
    logger.info("Generate training dataset")
    tag_rules = config["tag_rules"]
    stemming_rule_cutoff = config["stemming_rule_cutoff"]
    train_data, stem_rules, tags, discarded = construct_train_dataset(
        train_data, tag_rules, stemming_rule_cutoff
    )
    logger.info(f"Training data contains {len(train_data)} sents")
    logger.info(f"Discarded {discarded} invalid sents from train data")
    logger.info(f"Collected {len(stem_rules)} Stemming rules")
    logger.info(f"Collected {len(tags)} morphological tags")

    if tag_rules:
        logger.info("Stemming rules contain morphological tag")
    else:
        logger.info("Morphological tags are predicted separately from stems")

    if not test:
        evaluate_coverage(eval_data, stem_rules, logger, tag_rules)

    logger.info("Index dataset")

    # Build vocabulary and index the dataset
    indexed_train_data, indexed_eval_data, indexer = index_dataset(
        train_data, eval_data, stem_rules, tags, tag_rules
    )

    logger.info(f"{len(indexer.vocabulary)} chars in vocab:\n{indexer.vocabulary}\n")
    
    temp_data_path = os.path.join(config["cwd"], "temp_train_data_task2.pickle")
    with open(temp_data_path, "wb") as tf:
        pickle.dump(
            (indexed_train_data, indexed_eval_data, eval_data, stem_rules, tags, tag_rules, indexer), tf
        )
        
    start = time.time()
    
    if tune:
        scheduler = ASHAScheduler(metric="score", mode="max", max_t=max_num_epochs, grace_period=1, reduction_factor=2)
        reporter = CLIReporter(
            parameter_columns=[
                "epochs",
                "lr",
                "embedding_dim",
                "encoder_max_ngram",
            ],
            metric_columns=[
                "loss",
                "score",
                "training_iteration",
            ],  # we don't have accuracy now
            max_report_frequency=300,  # report every 5 min
        )

        # Tuning
        result = hyperparam_tune.run(partial(train_model, checkpoint_dir=config["checkpoint_dir"],),
                                     resources_per_trial={"cpu": 8, "gpu": gpus_per_trial},
                                     config=config,  # our search space
                                     num_samples=num_samples,
                                     scheduler=scheduler,
                                     progress_reporter=reporter,
                                     name="T1_tune",
                                     log_to_file=True,
                                     fail_fast=True,  # stopping after first failure
                                     # resume=True,
                                     )

        best_trial = result.get_best_trial("loss", "max", "last")
        logger.info(f"Best trial config: {best_trial.config}")
        logger.info(f"Best trial final validation loss: {best_trial.last_result['loss']}")
        best_trained_model = build_model(best_trial.config, indexer, tag_rules)
        
        with open("best_config_t1.pickle", "wb") as cf:
            pickle.dump(best_trial.config, cf)
        
        device = "cuda" if torch.cuda.is_available() else "cpu"
        best_trained_model.to(device)
        
        best_checkpoint_dir = best_trial.checkpoint.value
        model_state, optimizer_state = torch.load(
            Path(best_checkpoint_dir, "checkpoint")
        )
        best_trained_model.load_state_dict(model_state)
        model = best_trained_model

    else:
        model, optimizer = train_model(train_config)
        
    # (false) end of prediction
    duration = time.time() - start
    logger.info(f"Duration: {duration:.2f} seconds.\n")

    if test:
        logger.info("Creating predictions on test data")
        
        # Index test data
        indexed_test_data = []
        for raw_tokens, *_ in test_data:
            raw_tokens = raw_tokens.split()
            indexed_tokens = list(map(indexer.index_token, raw_tokens))
            indexed_test_data.append((raw_tokens, indexed_tokens))
        
        # Create dataloader
        test_dataloader = DataLoader(
            indexed_test_data,
            batch_size=64,
            collate_fn=eval_collate_fn,
            shuffle=False,
        )
        
        # Get predictions
        device = "cuda" if torch.cuda.is_available() and config["cuda"] else "cpu"
        predictions = evaluate_model(
            model, test_dataloader, indexer, device, tag_rules, translit
            )
        
        # Create submission
        logger.info("Create submission files")
        # save_task2_predictions(eval_predictions, duration)
        save_task2_predictions(predictions, duration)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Task 1")
    parser.add_argument(
        "--tune", action="store_true", help="whether to tune hyperparams"
    )
    args = parser.parse_args()

    tune = args.tune
    main(tune, num_samples=4, max_num_epochs=20, gpus_per_trial=1)  # test

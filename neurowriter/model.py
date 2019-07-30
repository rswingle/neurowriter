#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Fri Jul 28 11:26:32 2017

Module for creating, training and applying generation models.

@author: Álvaro Barbero Jiménez
"""

import copy
import logging
import math
import os
import pickle as pkl
from pytorch_transformers import BertForSequenceClassification, AdamW, WarmupLinearSchedule
from tempfile import NamedTemporaryFile
import torch
import torch.nn.functional as F
from tqdm import tqdm

from neurowriter.tokenizer import CLS, SEP, END


class Model:
    """Implements a text generation model that can be trained with a given Corpus"""

    def __init__(self):
        """Initializes a new Model. The model must be trained before text generation is possible"""
        self.model = None
        self.labels = []
        self.contextsize = None
        # Prepare GPU
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    def fit(self, dataset, outputdir, maxepochs=1000, patience=10, learningrate=5e-5, checkpointepochs=10, gradient_accumulation_steps=1):
        """Trains a keras model with given parameters
        
        Arguments
            dataset: dataset to use for training
            outputdir: directory in which to save model
            maxepochs: maximum allowed training epochs for each model
            patience: number of epochs without improvement for early stopping
            learningrate: learning rate to use in the optimizer
            checkpointepochs: every checkpointepochs the current model will be saved to disk
            gradient_accumulation_steps: accumulate gradient along n batches. Allows large batch traing with small GPUs
        """
        logging.info(f"Training with learningrate={learningrate}, batchsize={dataset.batchsize}x{gradient_accumulation_steps}")
        logging.info(f"Training batches {dataset.lentrainbatches}, validation batches {dataset.lenvalbatches}")

        # Check dataset
        if dataset.lentrainbatches == 0 or dataset.lenvalbatches == 0:
            raise ValueError("Insufficient data for training in the current setting")

        # Save dataset info into the model, which will be used later for generation
        self.labels = dataset.uniquetokens
        self.contextsize = dataset.tokensperpattern

        # Build model with input parameters
        self.model = BertForSequenceClassification.from_pretrained('bert-base-multilingual-cased', 
                                                                   num_labels=dataset.lenlabels)
        self.model.to(self.device)

        # Prepare optimizer and schedule (linear warmup and decay)
        # Reference: https://github.com/huggingface/pytorch-transformers/blob/master/examples/run_glue.py#L80
        no_decay = ['bias', 'LayerNorm.weight']
        optimizer_grouped_parameters = [
            {'params': [p for n, p in self.model.named_parameters() if not any(nd in n for nd in no_decay)], 'weight_decay': 0.0},
            {'params': [p for n, p in self.model.named_parameters() if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
            ]
        optimizer = AdamW(optimizer_grouped_parameters, lr=learningrate, eps=1e-8)
        t_total = maxepochs * dataset.lentrainbatches
        scheduler = WarmupLinearSchedule(optimizer, warmup_steps=0, t_total=t_total)

        global_step = 0
        best_eval_loss = math.inf
        no_improvement = 0
        best_model = None
        gradient_loss = 0
        self.model.zero_grad()
        for epoch in tqdm(range(maxepochs), desc="Epoch", total=maxepochs):
            train_loss = 0
            self.model.train()
            epoch_iterator = tqdm(dataset.trainbatches(), desc="Batch", total=dataset.lentrainbatches)
            for step, batch in enumerate(epoch_iterator):
                # Forward pass through network
                model_loss = self._process_batch(batch)
                train_loss += model_loss.mean().item()

                # Backpropagation
                model_loss.backward()
                torch.nn.utils.clip_grad_norm_(self.model.parameters(), 1.0)

                # Model update
                if (step + 1) % gradient_accumulation_steps == 0:
                    scheduler.step()
                    optimizer.step()
                    self.model.zero_grad()
                    global_step += 1

            train_loss /= dataset.lentrainbatches
            # Measure loss in validation set
            eval_loss = self.eval(dataset)

            # Reports
            lr = scheduler.get_lr()[0]
            logging.info(f"lr={lr}")
            logging.info(f"train_loss={train_loss}")
            logging.info(f"eval_loss={eval_loss}")

            # Generation sample
            sample = self.generate(dataset.tokenizer)
            logging.info(f"Generation sample: {sample}")

            # Check early stopping
            if eval_loss < best_eval_loss:
                best_eval_loss = eval_loss
                no_improvement = 0
                best_model = copy.deepcopy(self.model)
            else:
                no_improvement += 1
            if no_improvement >= patience:
                logging.info(f"No improvement after {patience} epochs, stopping training")
                break

            # Save model checkpoint
            if checkpointepochs is not None and epoch % checkpointepochs == 0:
                check_dir = os.path.join(outputdir, 'checkpoint-{}'.format(epoch))
                self.save(check_dir)

        # Save best model
        self.model = best_model
        model_dir = os.path.join(outputdir, 'best')
        self.save(model_dir)

        return self

    def eval(self, dataset):
        """Evaluates the performance of the model in a given dataset. The validation part of the dataset is used"""
        # Evaluation all data batches
        eval_loss = 0.0
        nb_eval_steps = 0
        self.model.eval()
        with torch.no_grad():
            for batch in tqdm(dataset.valbatches(), desc="Evaluation batch", total=dataset.lenvalbatches):
                tmp_eval_loss = self._process_batch(batch)
                eval_loss += tmp_eval_loss.mean().item()
                nb_eval_steps += 1

        eval_loss /= nb_eval_steps
        return eval_loss

    def _process_batch(self, batch):
        """Processes a batch of data through the model, return the model loss for that batch"""
        batch = tuple(t.to(self.device) for t in batch)
        inputs = {'input_ids':      batch[0],
                    'attention_mask': batch[1],
                    'token_type_ids': batch[2],
                    'labels':         batch[3]}
        ouputs = self.model(**inputs)
        return ouputs[0]

    def generate(self, tokenizer, seed="", maxlength=100, temperature=1):
        """Generates text using this trained model

        Arguments
            - tokenizer: tokenizer to use to split text.
            - seed: text seed to initialize generator. Default: empty string
            - maxlength: maximum length of generated text.
            - temperature: temperature of modified softmax, can be understood as the level of creativity
        """
        tokenized_context = tokenizer.encodetext(seed)
        generated = []
        self.model.eval()

        # Pretokenize some special symbols
        ENDidx = tokenizer.encodetext(END)[0]

        for _ in range(maxlength):
            tokens, mask, types = tokenizer.encode_bert(tokenized_context)
            inputs = {
                'input_ids':      torch.tensor([tokens]).to(self.device),
                'attention_mask': torch.tensor([mask]).to(self.device),
                'token_type_ids': torch.tensor([types]).to(self.device)
            }
            with torch.no_grad():
                logits = self.model(**inputs)[0]
                logits = logits / temperature
                log_probs = F.softmax(logits, dim=-1)
                predicted_index = torch.multinomial(log_probs, num_samples=1)[0][0].tolist()
                predicted_index = self.labels[predicted_index]
                # Stop if END token generated
                if predicted_index == ENDidx:
                    return tokenizer.decodeindexes(generated)
                tokenized_context.append(predicted_index)
                generated.append(predicted_index)
            if len(tokenized_context) > self.contextsize:
                tokenized_context.pop(0)
        return tokenizer.decodeindexes(generated)

    def save(self, savefolder):
        """Saves the model into the given folder
        
        Saves both the model weights and the assignment between tokenizer indexes 
        and the train dataset metadata
        """
        if not os.path.exists(savefolder):
            os.makedirs(savefolder)
        # Save model
        model_to_save = self.model.module if hasattr(self.model, 'module') else self.model  # Take care of distributed/parallel training
        model_to_save.save_pretrained(savefolder)
        # Save labels
        metadata = (self.labels, self.contextsize)
        with open(os.path.join(savefolder, 'labels.pkl'), 'wb') as f:
            pkl.dump(metadata, f)

    @classmethod
    def load(cls, loadfolder):
        """Loads a model from the given folder"""
        model = Model()

        # Load labels
        with open(os.path.join(loadfolder, 'labels.pkl'), 'rb') as f:
            metadata = pkl.load(f)
        model.labels, model.contextsize = metadata

        model.model = BertForSequenceClassification.from_pretrained(loadfolder, num_labels=len(model.labels))
        model.model.to(model.device)

        return model

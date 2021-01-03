#internal imports
from ssl_dali_distrib import SIMCLR
from transforms_dali import SimCLRFinetuneTrainDataTransform, SimCLRFinetuneValDataTransform
from encoders_dali import load_encoder

from pytorch_lightning.core.lightning import LightningModule
from pytorch_lightning import Trainer

import pytorch_lightning as pl
import shutil
import os
from os import path
import splitfolders
from pytorch_lightning.callbacks.early_stopping import EarlyStopping
from pl_bolts.models.self_supervised.evaluator import SSLEvaluator

from pytorch_lightning.loggers import WandbLogger

from typing import List, Optional
from pytorch_lightning.metrics import Accuracy

from pathlib import Path
from argparse import ArgumentParser


from sklearn.metrics import f1_score, accuracy_score

from nvidia.dali.plugin.pytorch import DALIGenericIterator
from nvidia.dali.plugin.base_iterator import LastBatchPolicy

import torch
from torch import nn
from torch.nn import functional as F
from torch import nn
from torch.optim import SGD


class finetuner(pl.LightningModule):

  def __init__(self, DATA_PATH, encoder, embedding_size, withhold, batch_size, val_split, hidden_dims, train_transform, val_transform, num_workers, lr, num_classes, image_size):
      super().__init__()
      self.DATA_PATH = DATA_PATH
      self.val_split = val_split
      self.batch_size = batch_size
      self.hidden_dims = hidden_dims
      self.train_transform = train_transform
      self.val_transform = val_transform
      self.num_workers = num_workers
      self.withhold = withhold
      self.encoder = encoder
      self.embedding_size = embedding_size
      self.lr = lr 
      self.num_classes = num_classes
      self.image_size = image_size
      
      #save config for checkpointing
      self.save_hyperparameters()
      
      #model stuff    
      self.train_acc = Accuracy()
      self.val_acc = Accuracy(compute_on_step=False)
      
      self.linear_layer = SSLEvaluator(
            n_input=self.embedding_size,
            n_classes=self.num_classes,
            p=0.1,
            n_hidden=self.hidden_dims
       )
            
  def setup(self, stage = 'train'):
      #used for setting up dali pipeline, run on every gpu
      if stage == 'inference':
          print('Running model in inference mode. Dali iterator will flow data, no labels')     
          num_samples = sum([len(files) for r, d, files in os.walk(f'{self.DATA_PATH}')])
          #each gpu gets its own DALI loader
          inference_pipeline = self.val_transform(DATA_PATH = f"{self.DATA_PATH}", input_height = self.image_size, batch_size = self.batch_size, num_threads = self.num_workers, device_id = self.global_rank, stage = stage)
          
          class LightningWrapper(DALIGenericIterator):
              def __init__(self, *kargs, **kvargs):
                  super().__init__(*kargs, **kvargs)

              def __next__(self):
                  out = super().__next__()
                  out = out[0]
                  return out[self.output_map[0]]

              def __len__(self):
                return num_samples//self.batch_size


          inference_labels = [f'im{i}' for i in range(1, train_pipeline.COPIES+1)]
          self.inference_loader = LightningWrapper(inference_pipeline, inference_labels, auto_reset=True, last_batch_policy = LastBatchPolicy.PARTIAL,  last_batch_padded = True)
          self.train_loader = None
          self.val_loader = None
          
          
      else:
          
          num_samples = sum([len(files) for r, d, files in os.walk(f'{self.DATA_PATH}/train')])
          #each gpu gets its own DALI loader
          train_pipeline = self.train_transform(DATA_PATH = f"{self.DATA_PATH}/train", input_height = self.image_size, batch_size = self.batch_size, num_threads = self.num_workers, device_id = self.global_rank)
          val_pipeline = self.val_transform(DATA_PATH = f"{self.DATA_PATH}/val", input_height = self.image_size, batch_size = self.batch_size, num_threads = self.num_workers, device_id = self.global_rank)


          class LightningWrapper(DALIGenericIterator):
              def __init__(self, *kargs, **kvargs):
                  super().__init__(*kargs, **kvargs)

              def __next__(self):
                  out = super().__next__()
                  out = out[0]
                  return out[self.output_map[0]], torch.squeeze(out[self.output_map[-1]])

              def __len__(self):
                return num_samples//self.batch_size


          train_labels = [f'im{i}' for i in range(1, train_pipeline.COPIES+1)]
          train_labels.append('label')

          val_labels = [f'im{i}' for i in range(1, val_pipeline.COPIES+1)]
          val_labels.append('label')

          size_train = sum([len(files) for r, d, files in os.walk(f'{self.DATA_PATH}/train')])
          self.train_loader = LightningWrapper(train_pipeline, train_labels, auto_reset=True, last_batch_policy = LastBatchPolicy.PARTIAL,  last_batch_padded = True)
          self.val_loader = LightningWrapper(val_pipeline, val_labels, auto_reset=True, last_batch_policy = LastBatchPolicy.PARTIAL,  last_batch_padded = True)

  def shared_step(self, batch):
      x, y = batch
      feats = self.encoder(x)[-1]
      feats = feats.view(feats.size(0), -1)
      logits = self.linear_layer(feats)
      loss = self.loss_fn(logits, y)
      return loss, logits, y

  def training_step(self, batch, batch_idx):
      loss, logits, y = self.shared_step(batch)
      acc = self.train_acc(logits, y)
      self.log('tloss', loss, prog_bar=True)
      self.log('tastep', acc, prog_bar=True)
      self.log('ta_epoch', self.train_acc)

      return loss

  def validation_step(self, batch, batch_idx):
      with torch.no_grad():
          loss, logits, y = self.shared_step(batch)
          acc = self.val_acc(logits, y)
          
      acc = self.val_acc(logits, y)
      self.log('val_loss', loss, prog_bar=True, sync_dist=True)
      self.log('val_acc_epoch', self.val_acc, prog_bar=True)
      self.log('val_acc_epoch', self.val_acc, prog_bar=True)
      return loss

  def loss_fn(self, logits, labels):
      return F.cross_entropy(logits, labels)

  def configure_optimizers(self):
      opt = SGD([
                {'params': self.encoder.parameters()},
                {'params': self.linear_layer.parameters(), 'lr': self.lr}
            ], lr=1e-4, momentum=0.9)
      
      return [opt]

  def train_dataloader(self):
      return self.train_loader
  
  def val_dataloader(self):
      return self.val_loader
  
  def inference_dataloader(self):
      return self.inference_loader
    
  def forward(self, x):
      feats = self.encoder(x)[-1]
      feats = feats.view(feats.size(0), -1)
      logits = self.linear_layer(feats)
      return nn.Softmax(dim = 1)(logits)
       
def cli_main():
    parser = ArgumentParser()
    parser.add_argument("--DATA_PATH", type=str, help="path to folders with images")
    parser.add_argument("--encoder", default=None, type=str, help="classifier checkpoint, SSL checkpoint or encoder from encoders_dali")
    parser.add_argument("--batch_size", default=128, type=int, help="batch size for SSL")
    parser.add_argument("--num_workers", default=1, type=int, help="number of workers to use to fetch data")
    parser.add_argument("--hidden_dims", default=128, type=int, help="hidden dimensions in classification layer added onto model for finetuning")
    parser.add_argument("--epochs", default=200, type=int, help="number of epochs to train model")
    parser.add_argument("--lr", default=0.1, type=float, help="learning rate for training the model classification head")
    parser.add_argument("--patience", default=-1, type=int, help="automatically cuts off training if validation does not drop for (patience) epochs. Leave blank to have no validation based early stopping.")
    parser.add_argument("--val_split", default=0.2, type=float, help="percent in validation data")
    parser.add_argument("--withhold_split", default=0, type=float, help="decimal from 0-1 representing how much of the training data to withold from either training or validation")
    parser.add_argument("--gpus", default=1, type=int, help="number of gpus to use for training")
    parser.add_argument("--log_name", type=str, help="name of project to log on wandb")
    parser.add_argument("--image_size", default=256, type=int, help="height of square image")
    
    args = parser.parse_args()
    DATA_PATH = args.DATA_PATH
    batch_size = args.batch_size
    num_workers = args.num_workers
    hidden_dims = args.hidden_dims
    epochs = args.epochs
    lr = args.lr
    patience = args.patience
    val_split = args.val_split
    withhold = args.withhold_split
    gpus = args.gpus
    encoder = args.encoder
    log_name = 'FineTune_' + args.log_name + '.ckpt'
    image_size = args.image_size
    
    wandb_logger = WandbLogger(name=log_name,project='SpaceForce')
    checkpointed = '.ckpt' in encoder    
    
    #preparing data for training by splitting
    
    if not (path.isdir(f"{DATA_PATH}/train") and path.isdir(f"{DATA_PATH}/val")): 
        shutil.rmtree(f'./split_data_{log_name[:-5]}', ignore_errors=True)
        splitfolders.ratio(DATA_PATH, output=f'./split_data_{log_name[:-5]}', ratio=(1-val_split-withhold, val_split, withhold), seed = 10)
        DATA_PATH = f'./split_data_{log_name[:-5]}'
        print(f'automatically splitting data into train and validation data {val_split} and withhold {withhold}')

    num_classes = len(os.listdir(f'{DATA_PATH}/train'))
    
    if checkpointed:
        print('Trying to initializing model as a finetuner checkpoint...')
        try:
            model = finetuner.load_from_checkpoint(checkpoint_path=encoder)
        except Exception as e:
            print('Did not initialize as a finetuner. Trying to initializing model as an SSL checkpoint...')
            try:
                simclr = SIMCLR.load_from_checkpoint(checkpoint_path=encoder)
                encoder = simclr.encoder
                embedding_size = simclr.embedding_size
                model = finetuner(encoder = encoder, embedding_size = embedding_size, withhold = withhold, DATA_PATH = DATA_PATH, batch_size = batch_size, val_split = val_split, hidden_dims = hidden_dims, train_transform = SimCLRFinetuneTrainDataTransform, val_transform = SimCLRFinetuneValDataTransform, num_workers = num_workers, lr = lr, num_classes = num_classes, image_size = image_size)
            except Exception as e:
                print(e)
                print('invalid checkpoint to initialize SIMCLR model. This checkpoint needs to include the encoder and projection and be of the SIMCLR class from this library. Will try to initialize just the encoder')
                checkpointed = False 
            
    elif not checkpointed:
        
        encoder, embedding_size = load_encoder(encoder)
 
        model = finetuner(encoder = encoder, embedding_size = embedding_size, withhold = withhold, DATA_PATH = DATA_PATH, batch_size = batch_size, val_split = val_split, hidden_dims = hidden_dims, train_transform = SimCLRFinetuneTrainDataTransform, val_transform = SimCLRFinetuneValDataTransform, num_workers = num_workers, lr = lr, num_classes = num_classes, image_size = image_size)
    
    cbs = []
    backend = 'ddp'
    
    if patience > 0:
        cb = EarlyStopping('val_loss', patience = patience)
        cbs.append(cb)
        
    trainer = Trainer(gpus=gpus, max_epochs = epochs, progress_bar_refresh_rate=5, callbacks = cbs, distributed_backend=f'{backend}' if args.gpus > 1 else None, logger = wandb_logger, enable_pl_optimizer=True)
    trainer.fit(model)
    
    Path(f"./models/FineTune").mkdir(parents=True, exist_ok=True)
    trainer.save_checkpoint(f"./models/FineTune/{log_name}")
    print("YOUR MODEL CAN BE ACCESSED AT: ", f"./models/FineTune/{log_name}")
    
if __name__ == '__main__':
    cli_main()

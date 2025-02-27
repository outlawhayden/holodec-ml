import os
import sys
import copy
import yaml
import tqdm
import torch
import pickle
import random
import logging
import itertools
import numpy as np

from torchvision.utils import save_image
from holodecml.torch.losses import *
from holodecml.torch.models import *
from holodecml.torch.optimizers import *


logger = logging.getLogger(__name__)


def LoadTrainer(train_gen, 
                valid_gen, 
                dataloader, 
                valid_dataloader, 
                device, 
                config):
    
    if "type" not in config["trainer"]:
        logger.warning("In order to load a model you must supply the type field.")
        raise OSError("Failed to load a trainer. Exiting")
        
    trainer_type = config["trainer"].pop("type")
    logger.info(f"Loading trainer-type {trainer_type}")
    
    if trainer_type in ["vae", "att-vae"]:
        return BaseTrainer(
            train_gen=train_gen,
            valid_gen=valid_gen,
            dataloader=dataloader,
            valid_dataloader=valid_dataloader,
            model_conf = config["model"], 
            optimizer_conf = config["optimizer"],
            device=device,
            **config["trainer"]
        )

    else:
        logger.info(
            f"Unsupported trainer type {trainer_type}. Choose from vae or att-vae. Exiting.")
        sys.exit(1)


class BaseTrainer:

    def __init__(self,
                 train_gen,
                 valid_gen,
                 dataloader,
                 valid_dataloader,
                 model_conf,
                 optimizer_conf,
                 start_epoch=0,
                 epochs=100,
                 batches_per_epoch = 1e10,
                 device="cpu",
                 clip=1.0,
                 alpha=1.0,
                 beta=1.0,
                 kld_weight=[],
                 path_save="./",
                 test_image=None,
                 training_loss=None,
                 save_test_image_every=1):
        
        # Initialize and build a model
        self.model = LoadModel(model_conf)
        self.model.build()
        self.model = self.model.to(device)
        
        # Initialize the optimizer
        self.optimizer = LoadOptimizer(
            optimizer_conf, 
            self.model.parameters()
        )
        
        self.train_gen = train_gen
        self.valid_gen = valid_gen
        self.dataloader = dataloader
        self.valid_dataloader = valid_dataloader
        self.batch_size = dataloader.batch_size
        self.batches_per_epoch = batches_per_epoch 
        self.path_save = path_save
        self.device = device
        self.start_epoch = start_epoch
        self.epochs = epochs

        self.alpha = alpha
        self.beta = beta

        self.kld_weight = kld_weight
        if len(kld_weight) == 0:
            self.kld_weight = [
                self.batch_size/self.train_gen.__len__(),
                self.valid_dataloader.batch_size/self.valid_gen.__len__()
            ]
            
        logger.info(f"Using mse/kld weights {self.alpha}/{self.beta} during training")
        
        
        if training_loss == "mae":
            self.criterion_train = SymmetricMAE(
                self.alpha, self.beta, self.kld_weight[0]
            )
        elif training_loss == "mse":  
            self.criterion_train = SymmetricMSE(
                self.alpha, self.beta, self.kld_weight[0]
            )
        elif training_loss == "logcosh":
            self.criterion_train = SymmetricLogCosh(
                self.alpha, self.beta, self.kld_weight[0]
            )
        elif training_loss == "xsigmoid":
            self.criterion_train = SymmetricXSigmoid(
                self.alpha, self.beta, self.kld_weight[0]
            )
        elif training_loss == "xtanh":
            self.criterion_train = SymmetricXTanh(
                self.alpha, self.beta, self.kld_weight[0]
            )
        else:
            self.criterion_train = SymmetricMSE(
                self.alpha, self.beta, self.kld_weight[0]
            )
            
        self.criterion_test = SymmetricMSE(
            1.0, 1.0, self.kld_weight[1]
        )

        self.test_image = test_image
        self.save_test_image_every = save_test_image_every

        # Gradient clipping through hook registration
        for p in self.model.parameters():
            if p.requires_grad:
                p.register_hook(lambda grad: torch.clamp(grad, -clip, clip))
        logger.info(f"Clipping gradients to range [-{clip}, {clip}]")

        # Create the save directory if it does not exist
        try:
            os.makedirs(path_save)
        except:
            pass

    def train_one_epoch(self, epoch):

        self.model.train()
        batches_per_epoch = int(
            np.ceil(self.train_gen.__len__() / self.batch_size))
        
        if batches_per_epoch > self.batches_per_epoch:
            batches_per_epoch = self.batches_per_epoch
        
        batch_group_generator = tqdm.tqdm(
            enumerate(self.dataloader),
            total=batches_per_epoch,
            leave=True
        )

        epoch_losses = {"loss": [], "bce": [], "kld": []}
        for idx, images in batch_group_generator:

            images = images.to(self.device)
            recon_images, mu, logvar = self.model(images)
            loss, bce, kld = self.criterion_train(
                recon_images, images, mu, logvar)

            self.optimizer.zero_grad()
            loss.backward()
            self.optimizer.step()

            batch_loss = loss.item()  # / self.batch_size
            bce_loss = bce.item()  # / self.batch_size
            kld_loss = kld.item()  # / self.batch_size

            epoch_losses["loss"].append(batch_loss)
            epoch_losses["bce"].append(bce_loss)
            epoch_losses["kld"].append(kld_loss)

            loss = np.mean(epoch_losses["loss"])
            bce = np.mean(epoch_losses["bce"])
            kld = np.mean(epoch_losses["kld"])

            to_print = "train_mse: {:.3f} train_bce: {:.3f} train_kld: {:.3f}".format(
                loss, bce, kld)
            batch_group_generator.set_description(to_print)
            batch_group_generator.update()
            
            if idx % batches_per_epoch == 0 and idx > 0:
                break
                
            if not np.isfinite(loss):
                logger.warning(f"Ending training early due to an exploding loss {loss}")
                break

        return loss, bce, kld

    def test(self, epoch):

        self.model.eval()
        batches_per_epoch = int(
            np.ceil(self.valid_gen.__len__() / self.valid_dataloader.batch_size))

        with torch.no_grad():

            batch_group_generator = tqdm.tqdm(
                enumerate(self.valid_dataloader),
                total=batches_per_epoch,
                leave=True
            )

            epoch_losses = {"loss": [], "bce": [], "kld": [], "mae": []}
            for idx, images in batch_group_generator:
                images = images.to(self.device)
                recon_images, mu, logvar = self.model(images)
                loss, bce, kld = self.criterion_test(
                    recon_images, images, mu, logvar)
                mae = nn.L1Loss()(recon_images, images)

                batch_loss = loss.item()  # / self.batch_size
                bce_loss = bce.item()  # / self.batch_size
                kld_loss = kld.item()  # / self.batch_size
                mae = mae.item()

                epoch_losses["loss"].append(batch_loss)
                epoch_losses["bce"].append(bce_loss)
                epoch_losses["kld"].append(kld_loss)
                epoch_losses["mae"].append(mae)
                

                loss = np.mean(epoch_losses["loss"])
                bce = np.mean(epoch_losses["bce"])
                kld = np.mean(epoch_losses["kld"])
                mae = np.mean(epoch_losses["mae"])

                to_print = "val_mse: {:.3f} val_mae: {:.3f} val_bce: {:.3f} val_kld: {:.3f}".format(
                    loss, mae, bce, kld)
                batch_group_generator.set_description(to_print)
                batch_group_generator.update()
                
                if not np.isfinite(loss):
                    logger.warning(f"Ending validation early due to an exploding loss {loss}")
                    break

            if isinstance(self.test_image, str):
                if os.path.isfile(self.test_image) and (epoch % self.save_test_image_every == 0):
                    try:
                        with open(self.test_image, "rb") as fid:
                            pic = pickle.load(fid)
                        self.compare(epoch, pic)
                    except:
                        pass

        return loss, mae, bce, kld

    def compare(self, epoch, x):
        x = x.to(self.device)
        recon_x, _, _ = self.model(x)
        if x.shape[0] > 1:
            for k, (_x, _x_recon_x) in enumerate(zip(x[:5], recon_x[:5])):
                _x = torch.unsqueeze(_x, 0)
                _x_recon_x = torch.unsqueeze(_x_recon_x, 0)
                compare_x = torch.cat([_x, _x_recon_x])
                save_image(compare_x.data.cpu(),
                           f'{self.path_save}/image_epoch_{epoch}_{k}.png')
        else:
            compare_x = torch.cat([x, recon_x])
            save_image(compare_x.data.cpu(),
                       f'{self.path_save}/image_epoch_{epoch}.png')

    def train(self,
              scheduler,
              early_stopping,
              metrics_logger):

        logger.info(
            f"Training the model for up to {self.epochs} epochs starting at epoch {self.start_epoch}"
        )

        flag = isinstance(
            scheduler, torch.optim.lr_scheduler.ReduceLROnPlateau)

        for epoch in range(self.start_epoch, self.epochs):

            train_loss, train_bce, train_kld = self.train_one_epoch(epoch)
            test_loss, test_mae, test_bce, test_kld = self.test(epoch)

            scheduler.step(test_loss if flag else epoch)
            early_stopping(epoch, test_loss, self.model, self.optimizer)

            # Write results to the callback logger
            result = {
                "epoch": epoch,
                "train_loss": train_loss,
                "train_bce": train_bce,
                "train_kld": train_kld,
                "valid_loss": test_loss,
                "valid_mae": test_mae,
                "valid_bce": test_bce,
                "valid_kld": test_kld,
                "lr": early_stopping.print_learning_rate(self.optimizer)
            }
            metrics_logger.update(result)

            if early_stopping.early_stop:
                logger.info("Early stopping")
                break
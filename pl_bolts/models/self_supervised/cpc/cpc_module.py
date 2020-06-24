"""
CPC V2
======
"""
import math
from argparse import ArgumentParser
from typing import Union

import pytorch_lightning as pl
import torch
import torch.nn.functional as F
import torch.optim as optim
from pytorch_lightning.utilities import rank_zero_warn
from torch.optim.lr_scheduler import MultiStepLR

import pl_bolts
from pl_bolts import metrics
from pl_bolts.datamodules import CIFAR10DataModule, STL10DataModule
from pl_bolts.models.self_supervised.cpc.transforms import (
    CPCTrainTransformsCIFAR10,
    CPCEvalTransformsCIFAR10,
    CPCTrainTransformsSTL10,
    CPCEvalTransformsSTL10,
    CPCTrainTransformsImageNet128,
    CPCEvalTransformsImageNet128
)

from pl_bolts.datamodules.ssl_imagenet_datamodule import SSLImagenetDataModule
from pl_bolts.losses.self_supervised_learning import CPCTask
from pl_bolts.models.self_supervised.cpc import transforms as cpc_transforms
from pl_bolts.models.self_supervised.cpc.networks import CPCResNet101
from pl_bolts.models.self_supervised.evaluator import SSLEvaluator
from pl_bolts.utils.pretrained_weights import load_pretrained
from pl_bolts.utils.ssl_utils import torchvision_ssl_encoder

__all__ = [
    'CPCV2'
]


class CPCV2(pl.LightningModule):

    def __init__(self,
                 datamodule: pl_bolts.datamodules.LightningDataModule = None,
                 encoder: Union[str, torch.nn.Module, pl.LightningModule] = 'cpc_encoder',
                 patch_size: int = 8,
                 patch_overlap: int = 4,
                 online_ft: int = True,
                 task: str = 'cpc',
                 num_workers: int = 4,
                 learning_rate: int = 1e-4,
                 data_dir: str = '',
                 meta_root: str = '',
                 batch_size: int = 32,
                 amdim_task=False,
                 pretrained: str = None,
                 **kwargs):
        """
        PyTorch Lightning implementation of `Data-Efficient Image Recognition with Contrastive Predictive Coding
        <https://arxiv.org/abs/1905.09272>`_

        Paper authors: (Olivier J. Hénaff, Aravind Srinivas, Jeffrey De Fauw, Ali Razavi,
        Carl Doersch, S. M. Ali Eslami, Aaron van den Oord).

        Model implemented by:

            - `William Falcon <https://github.com/williamFalcon>`_
            - `Tullie Murrel <https://github.com/tullie>`_

        Example:

            >>> from pl_bolts.models.self_supervised import CPCV2
            ...
            >>> model = CPCV2()

        Train::

            trainer = Trainer()
            trainer.fit(model)

        Some uses::

            # load resnet18 pretrained using CPC on imagenet
            model = CPCV2(encoder='resnet18', pretrained=True)
            resnet18 = model.encoder
            renset18.freeze()

            # it supportes any torchvision resnet
            model = CPCV2(encoder='resnet50', pretrained=True)

            # use it as a feature extractor
            x = torch.rand(2, 3, 224, 224)
            out = model(x)

        Args:
            datamodule: A Datamodule (optional). Otherwise set the dataloaders directly
            encoder: A string for any of the resnets in torchvision, or the original CPC encoder,
                or a custon nn.Module encoder
            patch_size: How big to make the image patches
            patch_overlap: How much overlap should each patch have.
            online_ft: Enable a 1024-unit MLP to fine-tune online
            task: Which self-supervised task to use ('cpc', 'amdim', etc...)
            num_workers: num dataloader worksers
            learning_rate: what learning rate to use
            data_dir: where to store data
            meta_root: path to the imagenet meta.bin file (if not inside your imagenet folder)
            batch_size: batch size
            pretrained: If true, will use the weights pretrained (using CPC) on Imagenet
        """

        super().__init__()
        self.save_hyperparameters()

        self.online_evaluator = self.hparams.online_ft

        if pretrained:
            self.hparams.dataset = pretrained
            self.online_evaluator = True

        # link data
        if datamodule is None:
            datamodule = CIFAR10DataModule(self.hparams.data_dir, num_workers=self.hparams.num_workers)
            datamodule.train_transforms = CPCTrainTransformsCIFAR10()
            datamodule.val_transforms = CPCEvalTransformsCIFAR10()
        self.datamodule = datamodule

        # init encoder
        self.encoder = encoder
        if isinstance(encoder, str):
            self.encoder = self.init_encoder()

        # info nce loss
        c, h = self.__compute_final_nb_c(self.hparams.patch_size)
        self.info_nce = CPCTask(num_input_channels=c, target_dim=64, embed_scale=0.1)

        if self.online_evaluator:
            z_dim = c * h * h
            num_classes = self.datamodule.num_classes
            self.non_linear_evaluator = SSLEvaluator(
                n_input=z_dim,
                n_classes=num_classes,
                p=0.2,
                n_hidden=1024
            )

        if pretrained:
            self.load_pretrained(encoder)

    def load_pretrained(self, encoder):
        available_weights = {'resnet18'}

        if encoder in available_weights:
            load_pretrained(self, f'CPCV2-{encoder}')
        elif available_weights not in available_weights:
            rank_zero_warn(f'{encoder} not yet available')

    def init_encoder(self):
        dummy_batch = torch.zeros((2, 3, self.hparams.patch_size, self.hparams.patch_size))

        encoder_name = self.hparams.encoder
        if encoder_name == 'cpc_encoder':
            return CPCResNet101(dummy_batch)
        else:
            return torchvision_ssl_encoder(encoder_name, return_all_feature_maps=self.hparams.amdim_task)

    def get_dataset(self, name):
        if name == 'cifar10':
            return CIFAR10DataModule(self.hparams.data_dir, num_workers=self.hparams.num_workers)
        elif name == 'stl10':
            return STL10DataModule(self.hparams.data_dir, num_workers=self.hparams.num_workers)
        elif name == 'imagenet2012':
            return SSLImagenetDataModule(
                self.hparams.data_dir,
                meta_root=self.hparams.meta_root,
                num_workers=self.hparams.num_workers
            )
        else:
            raise FileNotFoundError(f'the {name} dataset is not supported. Subclass \'get_dataset to provide'
                                    f'your own \'')

    def __compute_final_nb_c(self, patch_size):
        dummy_batch = torch.zeros((2 * 49, 3, patch_size, patch_size))
        dummy_batch = self.encoder(dummy_batch)

        # other encoders return a list
        if self.hparams.encoder != 'cpc_encoder':
            dummy_batch = dummy_batch[0]

        dummy_batch = self.__recover_z_shape(dummy_batch, 2)
        b, c, h, w = dummy_batch.size()
        return c, h

    def __recover_z_shape(self, Z, b):
        # recover shape
        Z = Z.squeeze(-1)
        nb_feats = int(math.sqrt(Z.size(0) // b))
        Z = Z.view(b, -1, Z.size(1))
        Z = Z.permute(0, 2, 1).contiguous()
        Z = Z.view(b, -1, nb_feats, nb_feats)

        return Z

    def forward(self, img_1):
        # put all patches on the batch dim for simultaneous processing
        b, p, c, w, h = img_1.size()
        img_1 = img_1.view(-1, c, w, h)

        # Z are the latent vars
        Z = self.encoder(img_1)

        # non cpc resnets return a list
        if self.hparams.encoder != 'cpc_encoder':
            Z = Z[0]

        # (?) -> (b, -1, nb_feats, nb_feats)
        Z = self.__recover_z_shape(Z, b)

        return Z

    def training_step(self, batch, batch_nb):
        # in STL10 we pass in both lab+unl for online ft
        if self.hparams.dataset == 'stl10':
            labeled_batch = batch[1]
            unlabeled_batch = batch[0]
            batch = unlabeled_batch

        img_1, y = batch

        # Latent features
        Z = self(img_1)

        # infoNCE loss
        nce_loss = self.info_nce(Z)
        loss = nce_loss
        log = {'train_nce_loss': nce_loss}

        # don't use the training signal, just finetune the MLP to see how we're doing downstream
        if self.online_evaluator:
            if self.hparams.dataset == 'stl10':
                img_1, y = labeled_batch

            with torch.no_grad():
                Z = self(img_1)

            # just in case... no grads into unsupervised part!
            z_in = Z.detach()

            z_in = z_in.reshape(Z.size(0), -1)
            mlp_preds = self.non_linear_evaluator(z_in)
            mlp_loss = F.cross_entropy(mlp_preds, y)
            loss = nce_loss + mlp_loss
            log['train_mlp_loss'] = mlp_loss

        result = {
            'loss': loss,
            'log': log
        }

        return result

    def validation_step(self, batch, batch_nb):

        # in STL10 we pass in both lab+unl for online ft
        if self.hparams.dataset == 'stl10':
            labeled_batch = batch[1]
            unlabeled_batch = batch[0]
            batch = unlabeled_batch

        img_1, y = batch

        # generate features
        # Latent features
        Z = self(img_1)

        # infoNCE loss
        nce_loss = self.info_nce(Z)
        result = {'val_nce': nce_loss}

        if self.online_evaluator:
            if self.hparams.dataset == 'stl10':
                img_1, y = labeled_batch
                Z = self(img_1)

            z_in = Z.reshape(Z.size(0), -1)
            mlp_preds = self.non_linear_evaluator(z_in)
            mlp_loss = F.cross_entropy(mlp_preds, y)
            acc = metrics.accuracy(mlp_preds, y)
            result['mlp_acc'] = acc
            result['mlp_loss'] = mlp_loss

        return result

    def validation_epoch_end(self, outputs):
        val_nce = metrics.mean(outputs, 'val_nce')

        log = {'val_nce_loss': val_nce}
        if self.online_evaluator:
            mlp_acc = metrics.mean(outputs, 'mlp_acc')
            mlp_loss = metrics.mean(outputs, 'mlp_loss')
            log['val_mlp_acc'] = mlp_acc
            log['val_mlp_loss'] = mlp_loss

        return {'val_loss': val_nce, 'log': log, 'progress_bar': log}

    def configure_optimizers(self):
        opt = optim.Adam(
            params=self.parameters(),
            lr=self.hparams.learning_rate,
            betas=(0.8, 0.999),
            weight_decay=1e-5,
            eps=1e-7
        )

        if self.hparams.dataset in ['cifar10', 'stl10']:
            lr_scheduler = MultiStepLR(opt, milestones=[250, 280], gamma=0.2)
        elif self.hparams.dataset == 'imagenet2012':
            lr_scheduler = MultiStepLR(opt, milestones=[30, 45], gamma=0.2)

        return [opt]  # , [lr_scheduler]

    def prepare_data(self):
        self.datamodule.prepare_data()

    def train_dataloader(self):
        loader = self.datamodule.train_dataloader(self.hparams.batch_size)
        return loader

    def val_dataloader(self):
        loader = self.datamodule.val_dataloader(self.hparams.batch_size)
        return loader

    @staticmethod
    def add_model_specific_args(parent_parser):
        parser = ArgumentParser(parents=[parent_parser], add_help=False)
        parser.add_argument('--online_ft', action='store_true')
        parser.add_argument('--task', type=str, default='cpc')
        parser.add_argument('--dataset', type=str, default='cifar10', help='cifar10, stl10, imagenet2012')

        (args, _) = parser.parse_known_args()

        # v100@32GB batch_size = 186
        cifar_10 = {
            'dataset': 'cifar10',
            'depth': 10,
            'patch_size': 8,
            'batch_size': 44,
            'nb_classes': 10,
            'patch_overlap': 8 // 2,
            'lr_options': [
                1e-5,
            ]
        }

        # v100@32GB batch_size = 176
        stl10 = {
            'dataset': 'stl10',
            'depth': 12,
            'patch_size': 16,
            'batch_size': 108,
            'nb_classes': 10,
            'bs_options': [
                176
            ],
            'patch_overlap': 16 // 2,
            'lr_options': [
                3e-5,
            ]
        }

        imagenet2012 = {
            'dataset': 'imagenet2012',
            'depth': 10,
            'patch_size': 32,
            'batch_size': 48,
            'nb_classes': 1000,
            'patch_overlap': 32 // 2,
            'lr_options': [
                2e-5,
            ]
        }

        DATASETS = {
            'cifar10': cifar_10,
            'stl10': stl10,
            'imagenet2012': imagenet2012
        }

        dataset = DATASETS[args.dataset]

        resnets = ['resnet18', 'resnet34', 'resnet50', 'resnet101', 'resnet152', 'resnext50_32x4d', 'resnext101_32x8d',
                   'wide_resnet50_2', 'wide_resnet101_2']
        parser.add_argument('--encoder', default='cpc_encoder', type=str)

        # dataset options
        parser.add_argument('--patch_size', default=dataset['patch_size'], type=int)
        parser.add_argument('--patch_overlap', default=dataset['patch_overlap'], type=int)

        # training params
        parser.add_argument('--batch_size', type=int, default=dataset['batch_size'])
        parser.add_argument('--learning_rate', type=float, default=0.0001)

        # data
        parser.add_argument('--data_dir', default='.', type=str)
        parser.add_argument('--meta_root', default='.', type=str)
        parser.add_argument('--num_workers', default=0, type=int)

        return parser


if __name__ == '__main__':
    pl.seed_everything(1234)
    parser = ArgumentParser()
    parser = pl.Trainer.add_argparse_args(parser)
    parser = CPCV2.add_model_specific_args(parser)

    args = parser.parse_args()
    args.online_ft = True

    if args.dataset == 'cifar10':
        datamodule = CIFAR10DataModule.from_argparse_args(args)
        datamodule.train_transforms = CPCTrainTransformsCIFAR10()
        datamodule.val_transforms = CPCEvalTransformsCIFAR10()

    if args.dataset == 'stl10':
        print('running STL-10')
        datamodule = STL10DataModule.from_argparse_args(args)
        datamodule.train_dataloader = datamodule.train_dataloader_mixed
        datamodule.val_dataloader = datamodule.val_dataloader_mixed
        datamodule.train_transforms = CPCTrainTransformsSTL10()
        datamodule.val_transforms = CPCEvalTransformsSTL10()

    if args.dataset == 'imagenet2012':
        datamodule = SSLImagenetDataModule.from_argparse_args(args)
        datamodule.train_transforms = CPCTrainTransformsImageNet128()
        datamodule.val_transforms = CPCEvalTransformsImageNet128()

    model = CPCV2(**vars(args), datamodule=datamodule)
    trainer = pl.Trainer.from_argparse_args(args)
    trainer.fit(model)

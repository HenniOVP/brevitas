# MIT License
#
# Copyright (c) 2019 Xilinx
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import random
import os
import time
from datetime import datetime

import torch
import torch.optim as optim
from torch import nn
from torch.optim.lr_scheduler import MultiStepLR
from torch.utils.data import DataLoader
from torchvision import transforms
from torchvision.datasets import MNIST, CIFAR10, CIFAR100

from .logger import Logger, TrainingEpochMeters, EvalEpochMeters
from .models import model_with_cfg
from .models.losses import SqrHingeLoss

from brevitas.onnx import FINNManager


def accuracy(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].flatten().float().sum(0)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res


class Trainer(object):
    def __init__(self, args):

        # Init arguments
        self.args = args
        experiment_name = '{}_{}_{}'.format(args.dataset, args.network, datetime.now().strftime('%Y%m%d_%H%M%S'))
        self.output_dir_path = os.path.join(args.experiments, experiment_name)

        if self.args.resume:
            self.output_dir_path, _ = os.path.split(args.resume)
            self.output_dir_path, _ = os.path.split(self.output_dir_path)

        if not args.dry_run:
            self.checkpoints_dir_path = os.path.join(self.output_dir_path, 'checkpoints')
            if not args.resume:
                os.mkdir(self.output_dir_path)
                os.mkdir(self.checkpoints_dir_path)
        self.logger = Logger(self.output_dir_path, args.dry_run)

        # Get requested dataset
        transform_to_tensor = transforms.Compose([transforms.ToTensor()])
        dataset = args.dataset
        if dataset == 'CIFAR10':
            train_transforms_list = [transforms.RandomCrop(32, padding=4),
                                     transforms.RandomHorizontalFlip(),
                                     transforms.ToTensor()]
            transform_train = transforms.Compose(train_transforms_list)
            builder = CIFAR10
            self.num_classes = 10
            self.in_channels = 3
        elif dataset == 'CIFAR100':
            train_transforms_list = [transforms.RandomCrop(32, padding=4),
                                     transforms.RandomHorizontalFlip(),
                                     transforms.ToTensor()]
            transform_train = transforms.Compose(train_transforms_list)
            builder = CIFAR100
            self.num_classes = 100
            self.in_channels = 3

        elif dataset == 'MNIST':
            transform_train = transform_to_tensor
            builder = MNIST
            self.num_classes = 10
            self.in_channels = 1
        else:
            raise Exception("Dataset not supported: {}".format(args.dataset))

        # Try to extract the correct model from save data
        try:
            model, cfg = model_with_cfg(args.network, args.pretrained)
            # Check that requested dataset matches and num classes
            msg = "Loaded model miss match, call arguments requested {}, but saved is {}"
            msg = msg.format(dataset, cfg.get('MODEL', 'DATASET'))
            assert dataset == cfg.get('MODEL', 'DATASET'), msg
            msg = "Loaded model num_classes miss match, call arguments requested {}, but saved is {}"
            msg = msg.format(self.num_classes, cfg.getint('MODEL', 'NUM_CLASSES'))
            assert self.num_classes == cfg.getint('MODEL', 'NUM_CLASSES'), msg
            self.logger.info("Loaded {} from original examples".format(args.network))
        except AssertionError as e:
            # Create requested model ourselves
            msg = "Could not load default model, creating new one as requested. The following assertion failed: {}"
            msg = msg.format(e)
            self.logger.warning(msg)
            # Extract architecture info, example string: LFC_1W1A
            arch = args.network.split("_")[0].upper()
            if arch not in model_impl_no_wrapper:
                raise Exception("Model not supported: {}".format(arch))
            # Instaciate network
            if arch == "CNV":
                in_bit_width = 8
            else:
                in_bit_width = 1
            model = model_impl_no_wrapper[arch]
            model = model(num_classes=self.num_classes,
                          weight_bit_width=args.weight_bit_width,
                          act_bit_width=args.act_bit_width,
                          in_bit_width=in_bit_width,
                          )
            msg = "Created fresh model for {}, with num_classes: {}, weight_bit_width: {}, act_bit_width: {}, in_bit_width: {}"
            msg = msg.format(args.network, self.num_classes, args.weight_bit_width, args.act_bit_width, in_bit_width)
            self.logger.info(msg)

        # Randomness
        random.seed(args.random_seed)
        torch.manual_seed(args.random_seed)
        torch.cuda.manual_seed_all(args.random_seed)

        # Datasets building

        train_set = builder(root=args.datadir,
                            train=True,
                            download=True,
                            transform=transform_train)
        test_set = builder(root=args.datadir,
                           train=False,
                           download=True,
                           transform=transform_to_tensor)
        self.train_loader = DataLoader(train_set,
                                       batch_size=args.batch_size,
                                       shuffle=True,
                                       num_workers=args.num_workers)
        self.test_loader = DataLoader(test_set,
                                      batch_size=args.batch_size,
                                      shuffle=False,
                                      num_workers=args.num_workers)

        # Init starting values
        self.starting_epoch = 1
        self.best_val_acc = 0

        # Setup device
        if args.gpus is not None:
            args.gpus = [int(i) for i in args.gpus.split(',')]
            self.device = 'cuda:' + str(args.gpus[0])
            torch.backends.cudnn.benchmark = True
        else:
            self.device = 'cpu'
        self.device = torch.device(self.device)

        # Resume checkpoint, if any
        if args.resume:
            print('Loading model checkpoint at: {}'.format(args.resume))
            package = torch.load(args.resume, map_location='cpu')
            model_state_dict = package['state_dict']
            model.load_state_dict(model_state_dict, strict=args.strict)

        if args.gpus is not None and len(args.gpus) == 1:
            model = model.to(device=self.device)
        if args.gpus is not None and len(args.gpus) > 1:
            model = nn.DataParallel(model, args.gpus)
        self.model = model

        # Loss function
        if args.loss == 'SqrHinge':
            self.criterion = SqrHingeLoss()
        else:
            self.criterion = nn.CrossEntropyLoss()
        self.criterion = self.criterion.to(device=self.device)

        # Init optimizer
        if args.optim == 'ADAM':
            self.optimizer = optim.Adam(self.model.parameters(),
                                        lr=args.lr,
                                        weight_decay=args.weight_decay)
        elif args.optim == 'SGD':
            self.optimizer = optim.SGD(self.model.parameters(),
                                       lr=self.args.lr,
                                       momentum=self.args.momentum,
                                       weight_decay=self.args.weight_decay)

        # Resume optimizer, if any
        if args.resume and not args.evaluate:
            self.logger.log.info("Loading optimizer checkpoint")
            if 'optim_dict' in package.keys():
                self.optimizer.load_state_dict(package['optim_dict'])
            if 'epoch' in package.keys():
                self.starting_epoch = package['epoch']
            if 'best_val_acc' in package.keys():
                self.best_val_acc = package['best_val_acc']

        # LR scheduler
        if args.scheduler == 'STEP':
            milestones = [int(i) for i in args.milestones.split(',')]
            self.scheduler = MultiStepLR(optimizer=self.optimizer,
                                         milestones=milestones,
                                         gamma=0.1)
        elif args.scheduler == 'FIXED':
            self.scheduler = None
        else:
            raise Exception("Unrecognized scheduler {}".format(self.args.scheduler))

        # Resume scheduler, if any
        if args.resume and not args.evaluate and self.scheduler is not None:
            self.scheduler.last_epoch = package['epoch'] - 1

    def quant_export(self, model, output_dir_path, model_name,
                     input_shape=(1, 3, 32, 32),
                     input_tensor=None, torch_onnx_kwargs={}):
        # move model to CPU otherwise the export fails
        model.to("cpu")
        FINNManager.export_onnx(module=model,
                            input_shape=input_shape,
                            export_path=output_dir_path + "/" + model_name + ".onnx",
                            input_t=input_tensor,
                            torch_onnx_kwargs=torch_onnx_kwargs)
        # move back to intended device
        model.to(device=self.device)

    def checkpoint_best(self, epoch, name):
        best_path = os.path.join(self.checkpoints_dir_path, name)
        self.logger.info("Saving checkpoint model to {}".format(best_path))
        torch.save({
            'state_dict': self.model.state_dict(),
            'optim_dict': self.optimizer.state_dict(),
            'epoch': epoch + 1,
            'best_val_acc': self.best_val_acc,
        }, best_path)
        self.quant_export(self.model, self.checkpoints_dir_path, name,
                          input_shape=(1, self.in_channels, 32, 32))

    def train_model(self):

        # training starts
        if self.args.detect_nan:
            torch.autograd.set_detect_anomaly(True)

        for epoch in range(self.starting_epoch, self.args.epochs):

            # Set to training mode
            self.model.train()
            self.criterion.train()

            # Init metrics
            epoch_meters = TrainingEpochMeters()
            start_data_loading = time.time()


            for i, data in enumerate(self.train_loader):
                (input, target) = data
                input = input.to(self.device, non_blocking=True)
                target = target.to(self.device, non_blocking=True)

                # for hingeloss only
                if isinstance(self.criterion, SqrHingeLoss):
                    target=target.unsqueeze(1)
                    target_onehot = torch.Tensor(target.size(0), self.num_classes).to(self.device, non_blocking=True)
                    target_onehot.fill_(-1)
                    target_onehot.scatter_(1, target, 1)
                    target=target.squeeze()
                    target_var = target_onehot
                else:
                    target_var = target

                # measure data loading time
                epoch_meters.data_time.update(time.time() - start_data_loading)

                # Training batch starts
                start_batch = time.time()
                output = self.model(input)
                loss = self.criterion(output, target_var)

                # compute gradient and do SGD step
                self.optimizer.zero_grad()
                loss.backward()
                self.optimizer.step()

                self.model.clip_weights(-1,1)

                # measure elapsed time
                epoch_meters.batch_time.update(time.time() - start_batch)

                if i % int(self.args.log_freq) == 0 or i == len(self.train_loader) - 1:
                    prec1, prec5 = accuracy(output.detach(), target, topk=(1, 5))
                    epoch_meters.losses.update(loss.item(), input.size(0))
                    epoch_meters.top1.update(prec1.item(), input.size(0))
                    epoch_meters.top5.update(prec5.item(), input.size(0))
                    self.logger.training_batch_cli_log(epoch_meters, epoch, i, len(self.train_loader))

                # training batch ends
                start_data_loading = time.time()

            # Set the learning rate
            if self.scheduler is not None:
                self.scheduler.step(epoch)
            else:
                # Set the learning rate
                if epoch%40==0:
                    self.optimizer.param_groups[0]['lr'] *= 0.5

            # Perform eval
            with torch.no_grad():
                top1avg = self.eval_model(epoch)

            # checkpoint
            if top1avg >= self.best_val_acc and not self.args.dry_run:
                self.best_val_acc = top1avg
                self.checkpoint_best(epoch, "best.tar")
            elif not self.args.dry_run:
                self.checkpoint_best(epoch, "checkpoint.tar")

        # training ends
        if not self.args.dry_run:
            return os.path.join(self.checkpoints_dir_path, "best.tar")

    def eval_model(self, epoch=None):
        eval_meters = EvalEpochMeters()

        # switch to evaluate mode
        self.model.eval()
        self.criterion.eval()

        for i, data in enumerate(self.test_loader):

            end = time.time()
            (input, target) = data

            input = input.to(self.device, non_blocking=True)
            target = target.to(self.device, non_blocking=True)
            
            # for hingeloss only
            if isinstance(self.criterion, SqrHingeLoss):        
                target=target.unsqueeze(1)
                target_onehot = torch.Tensor(target.size(0), self.num_classes).to(self.device, non_blocking=True)
                target_onehot.fill_(-1)
                target_onehot.scatter_(1, target, 1)
                target=target.squeeze()
                target_var = target_onehot
            else:
                target_var = target
            
            # compute output
            output = self.model(input)

            # measure model elapsed time
            eval_meters.model_time.update(time.time() - end)
            end = time.time()

            #compute loss
            loss = self.criterion(output, target_var)
            eval_meters.loss_time.update(time.time() - end)

            pred = output.data.argmax(1, keepdim=True)
            correct = pred.eq(target.data.view_as(pred)).sum()
            prec1 = 100. * correct.float() / input.size(0)

            _, prec5 = accuracy(output, target, topk=(1, 5))
            eval_meters.losses.update(loss.item(), input.size(0))
            eval_meters.top1.update(prec1.item(), input.size(0))
            eval_meters.top5.update(prec5.item(), input.size(0))

            #Eval batch ends
            self.logger.eval_batch_cli_log(eval_meters, i, len(self.test_loader))

        return eval_meters.top1.avg

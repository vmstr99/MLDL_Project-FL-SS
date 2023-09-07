import copy
import torch
import torch.nn.functional as F
import os
import numpy as np
from PIL import Image
from torch import optim, nn
from collections import defaultdict
from torch.utils.data import DataLoader
from utils.utils import get_scheduler
import matplotlib.pyplot as plt
from utils.utils import HardNegativeMining, MeanReduction
from torch import distributed
import torchvision.transforms
from utils.dist_utils import initialize_distributed, setup, find_free_port
import torch.optim.lr_scheduler as lr_scheduler
from torch.optim.lr_scheduler import LambdaLR


device = torch.device( 'cuda' if torch. cuda. is_available () else 'cpu')

checkpoints_loaded_executed = False

class Client:

    def __init__(self, args, dataset, model, test_client=False):
        self.args = args
        self.dataset = dataset
        self.name = self.dataset.client_name
        self.model = model
        self.train_loader = DataLoader(self.dataset, batch_size=self.args.bs, shuffle=True, drop_last=True) \
            if not test_client else None
        self.test_loader = DataLoader(self.dataset, batch_size=self.args.bs, shuffle=False)
        self.checkpoints_loaded_executed = False
        self.criterion = nn.CrossEntropyLoss(ignore_index=255, reduction='none')
        self.reduction = HardNegativeMining() if self.args.hnm else MeanReduction()
        
    def __str__(self):
        return self.name


    def get_model(self):
        return self.model


    @staticmethod
    def update_metric(metrics, outputs, labels, cur_step):
        _, prediction = outputs.max(dim=1)
        labels = labels.cpu().numpy()
        prediction = prediction.cpu().numpy()
        # print(f'update_metric_prediction_type = {type(prediction)}')
        # print(f'update_metric_prediction_shape = {prediction.shape}')

        # pred = prediction[0,:,:]
        # plt.imshow(pred)
        # plt.savefig('trial_imgs/pred{}.png'.format(cur_step))

        metrics.update(labels, prediction)

    def _get_outputs(self, images):
        if self.args.model == 'deeplabv3_mobilenetv2':
            return self.model(images)['out']
        if self.args.model == 'resnet18':
            return self.model(images)
        raise NotImplementedError

    def get_optimizer(self, net, lr, wd, momentum):
      optimizer = torch.optim.SGD(net.parameters(), lr=lr, weight_decay=wd, momentum=momentum)
      return optimizer


    def loss_function(self):
      loss_function = nn.CrossEntropyLoss()
      return loss_function

    def calc_losses(self, images, labels):
      if self.args.model == 'deeplabv3_mobilenetv2':

          outputs = self._get_outputs(images)
          #print(f'outputs_shape_BEFORE = {outputs.shape}')
          # print(f'output_shape = {outputs.shape}')
          # print(f'image_shape = {images.shape}')
          # print(f'labels_shape = {labels.shape}')
        
          loss_tot = self.reduction(self.criterion(outputs, labels), labels)
          dict_calc_losses = {'loss_tot': loss_tot}
      else:
          raise NotImplementedError

      return dict_calc_losses, outputs
      
    def handle_grad(self, loss_tot):
        pass

    def calc_loss_fed(dict_losses):
        return dict_losses
      
    def clip_grad(self):
        pass

    def generate_update(self):
        return copy.deepcopy(self.model.state_dict())


    def _configure_optimizer(self, params, current_round):
      if self.args.optimizer == 'SGD':
          optimizer = optim.SGD(params, lr=self.args.lr, momentum=self.args.m,
                                weight_decay=self.args.wd)
      elif self.args.optimizer == 'other':
          optimizer = optim.Adam(params, lr=self.args.lr, weight_decay=self.args.wd)
      
      # Define the learning rate lambda function for LAMBDALR scheduler
      lr_lambda = lambda epoch: 0.95 ** (current_round * self.args.num_epochs + epoch)
      #scheduler = LambdaLR(optimizer, lr_lambda=lr_lambda)
      #scheduler = lr_scheduler.LambdaLR(optimizer, lr_lambda=self.polynomial_decay)
      scheduler = lr_scheduler.StepLR(optimizer, step_size=5, gamma=0.1)
      
      return optimizer, scheduler

    def polynomial_decay(self, current_round):
        # Set the initial learning rate
        initial_lr = 0.1

        # Set the final learning rate
        final_lr = 0.0001

        # Set the power of the polynomial
        power = 2

        # Calculate the decay factor based on the current epoch and total number of epochs
        decay_factor = (1 - current_round / self.args.num_rounds) ** power

        # Calculate the learning rate for the current epoch
        lr = initial_lr + (final_lr - initial_lr) * decay_factor

        return lr

    def handle_log_loss(self, dict_all_epoch_losses, dict_losses_list):

        for n, l in dict_all_epoch_losses.items():

            dict_all_epoch_losses[n] = torch.tensor(l).to(device)
            #dict_losses_list[n].append(dict_all_epoch_losses[n])
        return dict_all_epoch_losses, dict_losses_list


    def run_epoch(self, cur_epoch, optimizer, metrics, scheduler=None):
        """
        This method locally trains the model with the dataset of the client. It handles the training at mini-batch level
        :param cur_epoch: current epoch of training
        :param optimizer: optimizer used for the local training
        """
        dict_all_epoch_losses = defaultdict(lambda: 0)

        for cur_step, (images, labels) in enumerate(self.train_loader):
            # TODO: missing code here!
            images = images.to(device, dtype=torch.float32)
            labels = labels.to(device, dtype=torch.long)
            # print(f'train_images_shape = {images.shape}')
            # print(f'train_output_shape = {labels.shape}')

            optimizer.zero_grad()
            dict_calc_losses, outputs = self.calc_losses(images, labels)
            #print(f'outputs after calculate_loss = {outputs.shape}')

            dict_calc_losses['loss_tot'].backward()
            self.handle_grad(dict_calc_losses['loss_tot'])

            self.clip_grad()
            optimizer.step()
            scheduler.step()

            if cur_epoch == self.args.num_epochs - 1:
              
              self.update_metric(metrics, outputs, labels, cur_step)

            print_string = ""
            for name, l in dict_calc_losses.items():
                  if type(l) != int:
                      dict_all_epoch_losses[name] += l.detach().item()
                  else:
                      dict_all_epoch_losses[name] += l


        for name, l in dict_all_epoch_losses.items():
          dict_all_epoch_losses[name] /= len(self.train_loader)
          print_string += f"{name}={'%.3f' % dict_all_epoch_losses[name]}, "
          print(print_string)

        return dict_all_epoch_losses, optimizer
            
        

    def train(self, metrics, current_round, opt, scheduler):
        """
        This method locally trains the model with the dataset of the client. It handles the training at epochs level
        (by calling the run_epoch method for each local epoch of training)
        :return: length of the local dataset, copy of the model parameters
        """
        num_train_samples = len(self.dataset)
        
        dict_losses_list = defaultdict(lambda: [])
        self.model.train()
        #bn_dict_tmp = None
        net = self.get_model()
        optimizer, scheduler = self._configure_optimizer(net.parameters(), current_round)
        #print(f'curr round = {current_round}')
        # print(f'load ckpt = {self.args.load_checkpoint}')
        # print(f'clinet flag= {self.checkpoints_loaded_executed}')
        # PATH = "/content/drive/MyDrive/MLDL23-FL-step5-fda-yolo2/checkpoints/model_step5.pt"
        # if (self.args.load_checkpoint) and  (not self.checkpoints_loaded_executed):
        #           print("loading checkpoints...")
        #           opt = optim.SGD(net.parameters(), lr=self.args.lr, momentum=self.args.m,
        #                               weight_decay=self.args.wd)
        #           checkpoint = torch.load(PATH)
        #           net.load_state_dict(checkpoint['model_state_dict'])
        #           opt.load_state_dict(checkpoint['optimizer_state_dict'])
        #           epoch = checkpoint['epoch']
        #           #loss = checkpoint['loss']
        #           scheduler = lr_scheduler.StepLR(opt, step_size=5, gamma=0.1)
        #           net.train()
        #           self.checkpoints_loaded_executed = True
        #           print("done")
        # else:
        #           opt, scheduler = self._configure_optimizer(net.parameters(), current_round)
        # opt = self.get_optimizer(net, lr=self.args.lr, wd=self.args.wd, momentum=self.args.m)
        # scheduler = lr_scheduler.StepLR(opt, step_size=5, gamma=0.1)
        
        #self.model.train()
        # TODO: missing code here!
        for epoch in range(self.args.num_epochs):
            # TODO: missing code here!
              
            dict_all_epoch_losses, optimizer = self.run_epoch(epoch, optimizer = opt, metrics=metrics, scheduler=scheduler)
            dict_all_epoch_losses, dict_losses_list = self.handle_log_loss(dict_all_epoch_losses, dict_losses_list)

        if epoch == self.args.num_epochs and self.args.save_checkpoints: 
            print("save checkpoints...")
            PATH = "/content/drive/MyDrive/MLDL23-FL-step5-fda-prova-facciaml/checkpoints/model_check.pt"
            torch.save({
                    'epoch': self.args.num_epochs,
                    'model_state_dict': self.model.state_dict(),
                    'optimizer_state_dict': optimizer.state_dict(),
                    }, PATH)

        #metrics.synch(self.device)

        update = self.generate_update()

        return num_train_samples, update, dict_losses_list, optimizer

    def test2(self, metric, yolo_masks):
        """
        This method tests the model on the local dataset of the client.
        :param metric: StreamMetric object
        """
        print("Test...")
        self.model.eval()
        class_loss = 0.0
        ret_samples = []

        with torch.no_grad():
            for i, sample in enumerate(self.test_loader):
              images, labels = sample
              
              images = images.to(device, dtype=torch.float32)
              labels = labels.to(device, dtype=torch.long)
              # print(f'test_images_shape = {images.shape}')
              # print(f'test_output_shape = {labels.shape}')
              outputs = self._get_outputs(images)
              # print(f'test_output_shape = {outputs.shape}')
              # print(f'test_output_shape = {labels.shape}')

              loss = self.reduction(self.criterion(outputs, labels),labels)
              class_loss += loss.item()

              _, prediction = outputs.max(dim=1)
              labels = labels.cpu().numpy()
              prediction = prediction.cpu().numpy()

              #yolo step
              pred2d = prediction[0,:,:]
              final_matrix = self.merge_matrices(pred2d, yolo_masks[i])
              metric['test_same_dom'].update(labels[0,:,:], final_matrix)

              #metric['test_same_dom'].update(labels, prediction)


              #if self.args.plot == True:
                  #pred2 = prediction[0,:,:]  # Select the first image from the batch
              pred2 = final_matrix
              plt.imshow(pred2)
              plt.savefig('yolo_combined_pred/pred{}.png'.format(i))

            class_loss = torch.tensor(class_loss).to(device)
            print(f'class_loss = {class_loss}')
            print(f'len labels {len(labels)}')
            print(f'len final_matrix {len(final_matrix)}')
            class_loss = class_loss / len(self.test_loader)

        return class_loss, ret_samples


      


#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Wed Nov 28 13:01:05 2018
This script creates and trains a next word predictor using an RNN encoder. Set Bi-directional to
False in the RNN config! (to prevent peaking at future timesteps making NWP trivial)

@author: danny
"""
from __future__ import print_function
from torch.optim import lr_scheduler

import argparse
import torch
import numpy as np
import sys
import os
import pickle
sys.path.append('/data/next_word_prediction/PyTorch/functions')

from encoders import nwp_rnn_encoder
from nwp_trainer import nwp_trainer

parser = argparse.ArgumentParser(description='Create and run an articulatory feature classification DNN')

# args concerning file location
parser.add_argument('-data_loc', type = str, default = '/data/databases/next_word_prediction/',
                    help = 'location of the feature file, default: /data/databases/next_word_prediction/train_nwp.txt')
parser.add_argument('-results_loc', type = str, default = '/data/next_word_prediction/PyTorch/gru_results/',
                    help = 'location to save the trained network parameters')
parser.add_argument('-dict_loc', type = str, default = '/data/next_word_prediction/PyTorch/nwp_indices',
                    help = 'location of the dictionary containing the mapping between the vocabulary and the embedding indices')
# args concerning training settings
parser.add_argument('-batch_size', type = int, default = 100, help = 'batch size, default: 128')
parser.add_argument('-lr', type = float, default = 0.5, help = 'learning rate, default:0.0001')
parser.add_argument('-n_epochs', type = int, default = 8, help = 'number of training epochs, default: 32')
parser.add_argument('-cuda', type = bool, default = True, help = 'use cuda (gpu), default: True')
parser.add_argument('-save_states', type = list, default = [1000, 3000, 10000, 30000, 100000, 300000, 1000000, 3000000, 6470000], 
                    help = 'points in training where the model parameters are saved')
# args concerning the database and which features to load
parser.add_argument('-gradient_clipping', type = bool, default = True, help ='use gradient clipping, default: True')
parser.add_argument('-seed', type = int, default = None, help = 'optional seed for the random components')

args = parser.parse_args()

# check if cuda is availlable and if user wants to run on gpu
cuda = args.cuda and torch.cuda.is_available()
if cuda:
    print('using gpu')
else:
    print('using cpu')

# check is there is a given random seed (list!). If not create one but print it so it can be used to replicate this run. 
if args.seed:
    np.random.seed(args.seed[0])
    torch.manual_seed(args.seed[1])
else:
    seed = np.random.randint(0, 2**32, 2)
    print('random seeds (numpy, torch): ' + str(seed))
    np.random.seed(seed[0])
    torch.manual_seed(seed[1])
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False

def load_obj(loc):
    with open(loc + '.pkl', 'rb') as f:
        return pickle.load(f)

# get the size of the dictionary for the embedding layer (pytorch crashes if the embedding layer is not correct for the dictionary size)
# add 1 for the zero or padding embedding
dict_size = len(load_obj(args.dict_loc)) + 1 
# config settings for the RNN
config = {'embed':{'num_embeddings': dict_size, 'embedding_dim': 400, 'sparse': False, 'padding_idx': 0}, 'max_len': 41,
               'rnn':{'input_size': 400, 'hidden_size': 500, 'num_layers': 1, 'batch_first': True,
               'bidirectional': False, 'dropout': 0}, 'lin1':{'input_size': 500, 'output_size': 400}, 'lin2':{'input_size': 400}}

def load(folder, file_name):
    open_file = open(os.path.join(folder, file_name))
    line = [x for x in open_file]  
    open_file.close()
    return line  
    
train = load(args.data_loc, 'train_nwp.txt')
print('#training samples: ' + str(len(train)))
# set some part of the dataset apart for validation and testing
#val = train[-700000:-350000]
#test = train[-350000:]
train = train[:3000]
############################### Neural network setup #################################################
# create the network and initialise the parameters to be xavier uniform distributed
nwp_model = nwp_rnn_encoder(config)

for p in nwp_model.parameters():
    if p.dim() > 1:
        torch.nn.init.xavier_uniform_(p)
    if p.dim() <=1:
        torch.nn.init.normal_(p)

model_parameters = filter(lambda p: p.requires_grad, nwp_model.parameters())
print('#model parameters: ' + str(sum([np.prod(p.size()) for p in model_parameters])))

# Adam optimiser. I found SGD to work terribly and could not find appropriate parameter settings for it.
optimizer = torch.optim.SGD(nwp_model.parameters(), lr = args.lr, momentum = 0.9)

#plateau_scheduler = lr_scheduler.ReduceLROnPlateau(optimizer, mode = 'min', factor = 0.2, patience = 0, 
#                                                   threshold = 0.0001, min_lr = 1e-5, cooldown = 0)
step_size = int(len(train)/(3 * args.batch_size))
step_scheduler = lr_scheduler.StepLR(optimizer, step_size, gamma=0.5, last_epoch=-1)

# cyclic scheduler which varies the learning rate between a min and max over a certain number of epochs
# according to a cosine function 
def create_cyclic_scheduler(max_lr, min_lr, stepsize):
    lr_lambda = lambda iteration: (max_lr - min_lr)*(0.5 * (np.cos(np.pi * (1 + (3 - 1) / stepsize * iteration)) + 1))+min_lr
    cyclic_scheduler = lr_scheduler.LambdaLR(optimizer, lr_lambda, last_epoch=-1)
    # lambda function which uses the cosine function to cycle the learning rate between the given min and max rates
    # the function operates between 1 and 3 (so the cos cycles from -1 to -1 ) normalise between 0 and 1 and then press between
    # min and max lr   
    return(cyclic_scheduler)

#cyclic_scheduler = create_cyclic_scheduler(max_lr = args.lr, min_lr = args.lr * 0.2, stepsize = int(len(train)/args.batch_size)*4)

# create a trainer setting the loss function, optimizer, minibatcher, lr_scheduler and the r@n evaluator
trainer = nwp_trainer(nwp_model)
trainer.set_dict_loc(args.dict_loc)
trainer.set_loss(torch.nn.CrossEntropyLoss(ignore_index= 0))
trainer.set_optimizer(optimizer)
trainer.set_token_batcher()
trainer.set_lr_scheduler(step_scheduler, 'cyclic')

#optionally use cuda and gradient clipping
if cuda:
    trainer.set_cuda()

# gradient clipping can help stabilise training in the first epoch.
if args.gradient_clipping:
    trainer.set_gradient_clipping(0.25)

################################# training/test loop #####################################=
# run the training loop for the indicated amount of epochs 
while trainer.epoch <= args.n_epochs:
    # Train on the train set    
    trainer.train_epoch(train, args.batch_size, args.save_states, args.results_loc)

    if args.gradient_clipping:
        # I found that updating the clip value at each epoch did not work well     
        # trainer.update_clip()
        trainer.reset_grads()
    #increase epoch#
    trainer.update_epoch()
    # reset the model for the next epoch
    for p in trainer.encoder.parameters():
        if p.dim() > 1:
            torch.nn.init.xavier_uniform_(p)
        if p.dim() <=1:
            torch.nn.init.normal_(p)

    optimizer = torch.optim.SGD(trainer.encoder.parameters(), lr = args.lr, momentum = 0.9)
    step_scheduler = lr_scheduler.StepLR(optimizer, step_size, gamma=0.5, last_epoch=-1)
    trainer.set_optimizer(optimizer)
    trainer.set_lr_scheduler(step_scheduler, 'cyclic')

# save the gradients for each epoch, can be usefull to select an initial clipping value.
if args.gradient_clipping:
    trainer.save_gradients(args.results_loc)


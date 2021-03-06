# pylint: disable=C, R, bare-except, arguments-differ, no-member, undefined-loop-variable, not-callable, unbalanced-tuple-unpacking, abstract-method
import argparse
import copy
import math
import os
import pickle
import subprocess
from functools import partial
from time import perf_counter

import torch

from train import train_model
from train import loss_fun
from arch import init_arch
from dataset import get_binary_dataset



def initialize_data():
    data = {}
    data['step'] = []
    data['t'] = []
    data['Train_loss'] = []
    data['Test_loss'] = []
    data['Train_aloss'] = []
    data['Test_aloss'] = []
    data['Train_error'] = []
    data['Test_error'] = []
    data['Train_grad_norm'] = []
    data['w_norm'] = []
    data['dw'] = []
    # dictionary with all interesting observables
    return data

def compute_observables(steps, time, model, grad_norm, xtr, ytr, otr0, xte, yte, ote0, loss, args, f_init):
    obs = {}
    y_pred_tr = model(xtr) - otr0
    y_pred_te = model(xte) - ote0

    w_norm = []
    dw = []
    with torch.no_grad():
        for w,w0 in zip(model.parameters(), f_init.parameters()):
            w_norm.append(w.norm().item())
            dw.append((w-w0).norm().item())
            
    obs['step'] = steps
    obs['t'] = time
    obs['Train_loss'] = loss(y_pred_tr, ytr).item()
    obs['Test_loss'] = loss(y_pred_te, yte).item()
    obs['Train_aloss'] = args['alpha'] * loss(y_pred_tr, ytr).item()
    obs['Test_aloss'] = args['alpha'] * loss(y_pred_te, yte).item()
    obs['Train_error'] = (ytr * args['alpha'] * y_pred_tr < 0).float().mean().item()
    obs['Test_error']  = (yte * args['alpha'] * y_pred_te < 0).float().mean().item()
    obs['Train_grad_norm'] = grad_norm
    obs['w_norm'] = w_norm
    obs['dw'] = dw
    # dictionary with all interesting observables
    return obs

  
def run_sgd(args, f_init, xtr, ytr, xte, yte):
    
    with torch.no_grad():
        ote0 = f_init(xte)
        otr0 = f_init(xtr)

    if args['f0'] == 0:
        ote0 = torch.zeros_like(ote0)
        otr0 = torch.zeros_like(otr0)

    # wall = perf_counter()

    # initialize
    loss = loss_fun(args['alpha'], args['loss'])
    model = copy.deepcopy(f_init)
    data = initialize_data() # dictionary with all interesting observables

    # compute and save things at initialization
    obs = compute_observables(0, 0.0, model, None, xtr, ytr, otr0, xte, yte, ote0, loss, args, f_init) # compute things for the current predictor
    for key in data.keys():
        data[key].append(obs[key])

    # print on screen
    print('[Step: {}, Time: {:.2e}] [Train aloss: {:.2e}, err: {:.2e}, grad_norm: {}] [Test aloss: {:.2e}, err: {:.2e}]'.format(obs['step'], obs['t'], obs['Train_aloss'], obs['Train_error'], obs['Train_grad_norm'], obs['Test_aloss'] , obs['Test_error']))

    # for p in model.parameters():
    #     print("p.requires_grad:", p.requires_grad)
    #     p.retain_grad()
    # loop over the predictors

    for internals in train_model(xtr, ytr, otr0, xte, yte, ote0, loss, model, True, args):
        steps, time, model, grad_norm = internals

        # compute things for the current predictor
        obs = compute_observables(steps, time, model, grad_norm, xtr, ytr, otr0, xte, yte, ote0, loss, args, f_init)

        # save things in the dictionary
        for key in data.keys():
            data[key].append(obs[key])

        # print on screen
        print('[Step: {}, Time: {:.2e}] [Train aloss: {:.2e}, err: {:.2e}, grad_norm: {:.2e}] [Test aloss: {:.2e}, err: {:.2e}]'.format(obs['step'], obs['t'], obs['Train_aloss'], obs['Train_error'], obs['Train_grad_norm'], obs['Test_aloss'] , obs['Test_error']))

        yield data # return the dictionary with all interesting observables

        if obs['Train_loss'] < 1e-6: break # stop training if train loss < threshold

    
def initialization(args):
    torch.backends.cudnn.benchmark = True
    if args['dtype'] == 'float64':
        torch.set_default_dtype(torch.float64)
    if args['dtype'] == 'float32':
        torch.set_default_dtype(torch.float32)

    [(xte, yte, ite), (xtk, ytk, itk), (xtr, ytr, itr)] = get_binary_dataset(
        args['dataset'],
        (args['pte'], args['ptk'], args['ptr']),
        (args['seed_testset'] + args['pte'], args['seed_kernelset'] + args['ptk'], args['seed_trainset'] + args['ptr']),
        args['d'],
        None,
        args['device'],
        torch.get_default_dtype()
    )

    f, (xtr, xtk, xte) = init_arch((xtr, xtk, xte), **args)

    return f, xtr, ytr, itr, xtk, ytk, itk, xte, yte, ite


def main():
    git = {
        'log': subprocess.getoutput('git log --format="%H" -n 1 -z'),
        'status': subprocess.getoutput('git status -z'),
    }
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", type=str, default=None)
    parser.add_argument("--dtype", type=str, default='float64')

    parser.add_argument("--seed_init", type=int, default=0)
    parser.add_argument("--seed_testset", type=int, default=-1, help="determines the testset, will affect the kernelset and trainset as well")
    parser.add_argument("--seed_trainset", type=int, default=None, help="determines the trainset")
    parser.add_argument("--seed_kernelset", type=int, default=0, help="determines the kernelset, will affect the trainset as well")
    parser.add_argument("--seed_batch", type=int, default=0)
   
    parser.add_argument("--dataset", type=str, required=True)
    parser.add_argument("--ptr", type=int, required=True)
    parser.add_argument("--pte", type=int)
    parser.add_argument("--ptk", type=int)
    parser.add_argument("--d", type=int)
    parser.add_argument("--data_param1", type=int,
                        help="Sphere dimension if dataset = Cylinder."
                        "Total number of cells, if dataset = sphere_grid. "
                        "n0 if dataset = signal_1d.")
    parser.add_argument("--data_param2", type=float,
                        help="Stretching factor for non-spherical dimensions if dataset = cylinder."
                        "Number of bins in theta, if dataset = sphere_grid.")
    
    parser.add_argument("--arch", type=str, required=True)
    parser.add_argument("--act", type=str, required=True)
    parser.add_argument("--act_beta", type=float, default=1.0)
    parser.add_argument("--bias", type=float, default=0)
    parser.add_argument("--last_bias", type=float, default=0)
    parser.add_argument("--var_bias", type=float, default=0)
    parser.add_argument("--L", type=int, default=None)
    parser.add_argument("--h", type=int, default=None)

    parser.add_argument("--alpha", type=float, required=True)
    parser.add_argument("--f0", type=int, default=1)
    
    parser.add_argument("--chunk", type=int)

    parser.add_argument("--loss", type=str, default="softhinge")
    parser.add_argument("--bs", type=int)
    parser.add_argument("--dt", type=float)
    
    parser.add_argument("--output", type=str, required=True)
    args = parser.parse_args()
    args = args.__dict__

    if args['device'] is None:
        if torch.cuda.is_available():
            args['device'] = 'cuda'
        else:
            args['device'] = 'cpu'

    if args['seed_trainset'] is None:
        args['seed_trainset'] = -int(args['seed_init']) - 2

    if args['pte'] is None:
        args['pte'] = args['ptr']

    if args['ptk'] is None:
        args['ptk'] = args['ptr']

    if args['seed_init'] == -1:
        args['seed_init'] = args['seed_trainset']

    with open(args['output'], 'wb') as handle:
        pickle.dump(args,  handle)

    saved = False

    f_init, xtr, ytr, itr, xtk, ytk, itk, xte, yte, ite = initialization(args)
    data = run_sgd(args, f_init, xtr, ytr, xte, yte)

    try:
        for data in run_sgd(args, f_init, xtr, ytr, xte, yte):
            # data['git'] = git
            with open(args['output'], 'wb') as handle:
                pickle.dump(args, handle)
                pickle.dump(data, handle)
            saved = True
    except:
        if not saved:
            os.remove(args['output'])
        raise
   
if __name__ == "__main__":
    main()

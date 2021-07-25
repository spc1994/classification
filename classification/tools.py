import os
import math
import numpy as np
import torch
import random
import yaml
from copy import deepcopy
from contextlib import contextmanager


def load_yaml(yaml_path):
    # Check and load yaml file
    if not os.path.isfile(yaml_path):
        raise ValueError('Load %s failed' % yaml_path)
    with open(yaml_path) as fd:
        yaml_dict = yaml.load(fd, Loader=yaml.SafeLoader)
    return yaml_dict


def save_yaml(file_path, yaml_dict):
    with open(file_path, 'w') as fd:
        yaml.dump(yaml_dict,
                  fd,
                  sort_keys=False)


def select_device(device):
    if isinstance(device, int):
        device = '%d' % device
    if device.lower() == 'cpu':
        # Force torch.cuda.is_available() to False
        os.environ['CUDA_VISIBLE_DEVICES'] = '-1'
    else:
        os.environ['CUDA_VISIBLE_DEVICES'] = device
    device = 'cpu'
    if torch.cuda.is_available():
        # Select device 0 for only one visible device
        device = 'cuda:0'
    return torch.device(device)


def torch_benchmark(benchmark=False, local_rank=-1):
    # Setup fixed or random training seed
    fixed_seed = local_rank + 1
    torch.manual_seed(fixed_seed)
    random.seed(fixed_seed)
    np.random.seed(fixed_seed)
    if benchmark:
        # Train slower but more reproducible
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True
    else:
        # Train faster but less reproducible
        torch.backends.cudnn.benchmark = True
        torch.backends.cudnn.deterministic = False


@contextmanager
def torch_zero_rank_first(local_rank):
    # https://github.com/ultralytics/yolov5
    # Make sure only the first local process in DDP process first
    # The following others can use the cache
    if local_rank not in [-1, 0]:
        torch.distributed.barrier()
    yield
    if local_rank == 0:
        torch.distributed.barrier()


def decay_lambda(final_ratio, 
                 total_steps, 
                 linear_decay=False):
    # Linear or cosine decay
    if linear_decay:
        lambda_fn = lambda x: final_ratio + (1.0 - final_ratio) * (
            1.0 - x / total_steps)
    else:
        lambda_fn = lambda x: 1.0 + (final_ratio - 1.0) * (
                (1.0 - math.cos(x * math.pi / total_steps)) / 2)
    return lambda_fn


def find_directory(root_path, base_name):
    found_path = False
    index = 0
    while not found_path:
        index += 1
        save_path = '%s/%s%d' % (root_path,
                                 base_name,
                                 index)
        if not os.path.exists(save_path):
            found_path = True
    return save_path


class ModelEMA(object):
    # https://github.com/rwightman/pytorch-image-models
    def __init__(self, model, decay=0.9999):
        self.model = deepcopy(model).eval()
        self.updates = 0
        # Decay exponential ramp (to help early epochs)
        self.decay_lambda = lambda x: decay * (1 - math.exp(-x / 2000))
        for p in self.model.parameters():
            p.requires_grad = False

    def update(self, model):
        # Update EMA parameters
        with torch.no_grad():
            self.updates += 1
            decay = self.decay_lambda(self.updates)
            state_dict = model.state_dict()
            for k, v in self.model.state_dict().items():
                if v.dtype.is_floating_point:
                    v *= decay
                    v += (1.0 - decay) * state_dict[k].detach()


def strip_optimizer(ckpt_path):
    # Strip optimizer from ckpt_path to finalize training
    device = torch.device('cpu')
    ckpt = torch.load(ckpt_path, map_location=device)
    if ckpt.get('model_ema'):
        ckpt['model'] = ckpt['model_ema']
    ckpt['model_ema'] = None
    ckpt['updates'] = None
    ckpt['optimizer'] = None
    ckpt['epoch'] = -1
    if torch.cuda.is_available():
        ckpt['model'].half()
    for p in ckpt['model'].parameters():
        p.requires_grad = False
    torch.save(ckpt, ckpt_path)
    ckpt_size = os.path.getsize(ckpt_path) / 1e6
    print('Optimizer stripped from %s, '
          'total file size %g MB' % (ckpt_path, ckpt_size))

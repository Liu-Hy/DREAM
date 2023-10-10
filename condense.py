import os
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torchvision import datasets, transforms
from data import transform_imagenet, transform_cifar, transform_svhn, transform_mnist, transform_fashion
from data import TensorDataset, ImageFolder, save_img
from data import ClassDataLoader, ClassMemDataLoader, MultiEpochsDataLoader
from data import MEANS, STDS
from train import define_model, train_epoch
from test import test_data, load_ckpt
from misc.augment import DiffAug
from misc import utils
import math
from math import ceil
import glob
from utils import get_strategy
from data import Data
from get_dp import get_noise_multiplier
class Synthesizer():
    """Condensed data class
    """
    def __init__(self, args, nclass, nchannel, hs, ws, device='cuda'):
        self.ipc = args.ipc
        self.nclass = nclass
        self.nchannel = nchannel
        self.size = (hs, ws)
        self.device = device

        self.data = torch.randn(size=(self.nclass * self.ipc, self.nchannel, hs, ws),
                                dtype=torch.float,
                                requires_grad=True,
                                device=self.device)
        self.data.data = torch.clamp(self.data.data / 4 + 0.5, min=0., max=1.)
        self.targets = torch.tensor([np.ones(self.ipc) * i for i in range(nclass)],
                                    dtype=torch.long,
                                    requires_grad=False,
                                    device=self.device).view(-1)
        self.cls_idx = [[] for _ in range(self.nclass)]
        for i in range(self.data.shape[0]):
            self.cls_idx[self.targets[i]].append(i)

        print("\nDefine synthetic data: ", self.data.shape)

        self.factor = max(1, args.factor)
        self.decode_type = args.decode_type
        self.resize = nn.Upsample(size=self.size, mode='bilinear')
        print(f"Factor: {self.factor} ({self.decode_type})")



    def init(self, loader, model, init_type='noise'):
        """Condensed data initialization
        """
        
        if init_type == 'random':
            print("Random initialize synset")
            for c in range(self.nclass):
                img, _ = loader.class_sample(c, self.ipc)
                self.data.data[self.ipc * c:self.ipc * (c + 1)] = img.data.to(self.device)

        elif init_type == 'mix':
            print("Mixed initialize synset")
            for c in range(self.nclass):
                img, _ = loader.class_sample(c, self.ipc * self.factor**2)
                img = img.data.to(self.device)

                s = self.size[0] // self.factor
                remained = self.size[0] % self.factor
                k = 0
                n = self.ipc

                h_loc = 0
                for i in range(self.factor):
                    h_r = s + 1 if i < remained else s
                    w_loc = 0
                    for j in range(self.factor):
                        w_r = s + 1 if j < remained else s
                        img_part = F.interpolate(img[k * n:(k + 1) * n], size=(h_r, w_r))
                        self.data.data[n * c:n * (c + 1), :, h_loc:h_loc + h_r,
                                       w_loc:w_loc + w_r] = img_part
                        w_loc += w_r
                        k += 1
                    h_loc += h_r

        elif init_type == 'noise':
            pass

    
    def parameters(self):
        parameter_list = [self.data]
        return parameter_list

    def subsample(self, data, target, max_size=-1):
        if (data.shape[0] > max_size) and (max_size > 0):
            indices = np.random.permutation(data.shape[0])
            data = data[indices[:max_size]]
            target = target[indices[:max_size]]

        return data, target

    def decode_zoom(self, img, target, factor):
        """Uniform multi-formation
        """
        h = img.shape[-1]
        remained = h % factor
        if remained > 0:
            img = F.pad(img, pad=(0, factor - remained, 0, factor - remained), value=0.5)
        s_crop = ceil(h / factor)
        n_crop = factor**2

        cropped = []
        for i in range(factor):
            for j in range(factor):
                h_loc = i * s_crop
                w_loc = j * s_crop
                cropped.append(img[:, :, h_loc:h_loc + s_crop, w_loc:w_loc + s_crop])
        cropped = torch.cat(cropped)
        data_dec = self.resize(cropped)
        target_dec = torch.cat([target for _ in range(n_crop)])

        return data_dec, target_dec

    def decode_zoom_multi(self, img, target, factor_max):
        """Multi-scale multi-formation
        """
        data_multi = []
        target_multi = []
        for factor in range(1, factor_max + 1):
            decoded = self.decode_zoom(img, target, factor)
            data_multi.append(decoded[0])
            target_multi.append(decoded[1])

        return torch.cat(data_multi), torch.cat(target_multi)

    def decode_zoom_bound(self, img, target, factor_max, bound=128):
        """Uniform multi-formation with bounded number of synthetic data
        """
        bound_cur = bound - len(img)
        budget = len(img)

        data_multi = []
        target_multi = []

        idx = 0
        decoded_total = 0
        for factor in range(factor_max, 0, -1):
            decode_size = factor**2
            if factor > 1:
                n = min(bound_cur // decode_size, budget)
            else:
                n = budget

            decoded = self.decode_zoom(img[idx:idx + n], target[idx:idx + n], factor)
            data_multi.append(decoded[0])
            target_multi.append(decoded[1])

            idx += n
            budget -= n
            decoded_total += n * decode_size
            bound_cur = bound - decoded_total - budget

            if budget == 0:
                break

        data_multi = torch.cat(data_multi)
        target_multi = torch.cat(target_multi)
        return data_multi, target_multi

    def decode(self, data, target, bound=128):
        """Multi-formation
        """
        if self.factor > 1:
            if self.decode_type == 'multi':
                data, target = self.decode_zoom_multi(data, target, self.factor)
            elif self.decode_type == 'bound':
                data, target = self.decode_zoom_bound(data, target, self.factor, bound=bound)
            else:
                data, target = self.decode_zoom(data, target, self.factor)

        return data, target

    def sample(self, c, max_size=128):
        """Sample synthetic data per class
        """
        idx_from = self.ipc * c
        idx_to = self.ipc * (c + 1)
        data = self.data[idx_from:idx_to]
        target = self.targets[idx_from:idx_to]

        data, target = self.decode(data, target, bound=max_size)
        data, target = self.subsample(data, target, max_size=max_size)
        return data, target

    def loader(self, args, augment=True):
        """Data loader for condensed data
        """
        if args.dataset == 'imagenet':
            train_transform, _ = transform_imagenet(augment=augment,
                                                    from_tensor=True,
                                                    size=0,
                                                    rrc=args.rrc,
                                                    rrc_size=self.size[0])
        elif args.dataset[:5] == 'cifar':
            train_transform, _ = transform_cifar(augment=augment, from_tensor=True)
        elif args.dataset == 'svhn':
            train_transform, _ = transform_svhn(augment=augment, from_tensor=True)
        elif args.dataset == 'mnist':
            train_transform, _ = transform_mnist(augment=augment, from_tensor=True)
        elif args.dataset == 'fashion':
            train_transform, _ = transform_fashion(augment=augment, from_tensor=True)

        data_dec = []
        target_dec = []
        for c in range(self.nclass):
            idx_from = self.ipc * c
            idx_to = self.ipc * (c + 1)
            data = self.data[idx_from:idx_to].detach()
            target = self.targets[idx_from:idx_to].detach()
            data, target = self.decode(data, target)

            data_dec.append(data)
            target_dec.append(target)

        data_dec = torch.cat(data_dec)
        target_dec = torch.cat(target_dec)

        train_dataset = TensorDataset(data_dec.cpu(), target_dec.cpu(), train_transform)

        print("Decode condensed data: ", data_dec.shape)
        nw = 0 if not augment else args.workers
        train_loader = MultiEpochsDataLoader(train_dataset,
                                             batch_size=args.batch_size,
                                             shuffle=True,
                                             num_workers=nw,
                                             persistent_workers=nw > 0)
        return train_loader

    def test(self, args, val_loader, logger, bench=True):
        """Condensed data evaluation
        """
        loader = self.loader(args, args.augment)
        test_data(args, loader, val_loader, test_resnet=False, logger=logger)

        if bench and not (args.dataset in ['mnist', 'fashion']):
            test_data(args, loader, val_loader, test_resnet=True, logger=logger)


def load_resized_data(args):
    """Load original training data (fixed spatial size and without augmentation) for condensation
    """
    if args.dataset == 'cifar10':
        train_dataset = datasets.CIFAR10(args.data_dir, download=True, train=True, transform=transforms.ToTensor())
        normalize = transforms.Normalize(mean=MEANS['cifar10'], std=STDS['cifar10'])
        transform_test = transforms.Compose([transforms.ToTensor(), normalize])
        val_dataset = datasets.CIFAR10(args.data_dir, download=True,train=False, transform=transform_test)
        train_dataset.nclass = 10

    elif args.dataset == 'cifar100':
        train_dataset = datasets.CIFAR100(args.data_dir,download=True,
                                          train=True,
                                          transform=transforms.ToTensor())

        normalize = transforms.Normalize(mean=MEANS['cifar100'], std=STDS['cifar100'])
        transform_test = transforms.Compose([transforms.ToTensor(), normalize])
        val_dataset = datasets.CIFAR100(args.data_dir, download=True,train=False, transform=transform_test)
        train_dataset.nclass = 100

    elif args.dataset == 'svhn':
        train_dataset = datasets.SVHN(os.path.join(args.data_dir, 'svhn'),
                                      split='train',download=True,
                                      transform=transforms.ToTensor())
        train_dataset.targets = train_dataset.labels

        normalize = transforms.Normalize(mean=MEANS['svhn'], std=STDS['svhn'])
        transform_test = transforms.Compose([transforms.ToTensor(), normalize])

        val_dataset = datasets.SVHN(os.path.join(args.data_dir, 'svhn'),
                                    split='test',download=True,
                                    transform=transform_test)
        train_dataset.nclass = 10

    elif args.dataset == 'mnist':
        train_dataset = datasets.MNIST(args.data_dir, download=True,train=True, transform=transforms.ToTensor())

        normalize = transforms.Normalize(mean=MEANS['mnist'], std=STDS['mnist'])
        transform_test = transforms.Compose([transforms.ToTensor(), normalize])

        val_dataset = datasets.MNIST(args.data_dir,download=True, train=False, transform=transform_test)
        train_dataset.nclass = 10

    elif args.dataset == 'fashion':
        train_dataset = datasets.FashionMNIST(args.data_dir,
                                              train=True,download=True,
                                              transform=transforms.ToTensor())

        normalize = transforms.Normalize(mean=MEANS['fashion'], std=STDS['fashion'])
        transform_test = transforms.Compose([transforms.ToTensor(), normalize])

        val_dataset = datasets.FashionMNIST(args.data_dir, download=True,train=False, transform=transform_test)
        train_dataset.nclass = 10

    elif args.dataset == 'imagenet':
        traindir = os.path.join(args.imagenet_dir, 'train')
        valdir = os.path.join(args.imagenet_dir, 'val')

        # We preprocess images to the fixed size (default: 224)
        resize = transforms.Compose([
            transforms.Resize(args.size),
            transforms.CenterCrop(args.size),
            transforms.PILToTensor()
        ])

        if args.load_memory:  # uint8
            transform = None
            load_transform = resize
        else:
            transform = transforms.Compose([resize, transforms.ConvertImageDtype(torch.float)])
            load_transform = None

        _, test_transform = transform_imagenet(size=args.size)
        train_dataset = ImageFolder(traindir,
                                    transform=transform,
                                    nclass=args.nclass,
                                    phase=args.phase,
                                    seed=args.dseed,
                                    load_memory=args.load_memory,
                                    load_transform=load_transform)
        val_dataset = ImageFolder(valdir,
                                  test_transform,
                                  nclass=args.nclass,
                                  phase=args.phase,
                                  seed=args.dseed,
                                  load_memory=False)

    val_loader = MultiEpochsDataLoader(val_dataset,
                                       batch_size=args.batch_size // 2,
                                       shuffle=False,
                                       persistent_workers=True,
                                       num_workers=4)

    assert train_dataset[0][0].shape[-1] == val_dataset[0][0].shape[-1]  # width check

    return train_dataset, val_loader


def remove_aug(augtype, remove_aug):
    aug_list = []
    for aug in augtype.split("_"):
        if aug not in remove_aug.split("_"):
            aug_list.append(aug)

    return "_".join(aug_list)


def diffaug(args, device='cuda'):
    """Differentiable augmentation for condensation
    """
    aug_type = args.aug_type
    normalize = utils.Normalize(mean=MEANS[args.dataset], std=STDS[args.dataset], device=device)
    print("Augmentataion Matching: ", aug_type)
    augment = DiffAug(strategy=aug_type, batch=True)
    aug_batch = transforms.Compose([normalize, augment])

    if args.mixup_net == 'cut':
        aug_type = remove_aug(aug_type, 'cutout')
    print("Augmentataion Net update: ", aug_type)
    augment_rand = DiffAug(strategy=aug_type, batch=False)
    aug_rand = transforms.Compose([normalize, augment_rand])

    return aug_batch, aug_rand


def dist(x, y, method='mse'):
    """Distance objectives
    """
    if method == 'mse':
        dist_ = (x - y).pow(2).sum()
    elif method == 'l1':
        dist_ = (x - y).abs().sum()
    elif method == 'l1_mean':
        n_b = x.shape[0]
        dist_ = (x - y).abs().reshape(n_b, -1).mean(-1).sum()
    elif method == 'cos':
        x = x.reshape(x.shape[0], -1)
        y = y.reshape(y.shape[0], -1)
        dist_ = torch.sum(1 - torch.sum(x * y, dim=-1) /
                          (torch.norm(x, dim=-1) * torch.norm(y, dim=-1) + 1e-6))

    return dist_


def add_loss(loss_sum, loss):
    if loss_sum == None:
        return loss
    else:
        return loss_sum + loss


def matchloss(args, img_real, img_syn, lab_real, lab_syn, model):
    """Matching losses (feature or gradient)
    """
    # max_grad_norm = 32.6355914473533 + 2 * (2.75161717041003 ** 0.5)
    # noise_multiplier = 1.53076171875
    max_grad_norm = args.max_grad_norm_a
    noise_multiplier = args.sigma_a
    loss = None
    if args.match == 'feat':
        with torch.no_grad():
            feat_tg = model.get_feature(img_real, args.idx_from, args.idx_to)
        feat = model.get_feature(img_syn, args.idx_from, args.idx_to)
        for i in range(len(feat)):
            loss = add_loss(loss, dist(feat_tg[i].mean(0), feat[i].mean(0), method=args.metric))

    elif args.match == 'grad':
        criterion = nn.CrossEntropyLoss()

        if args.dp_a or args.stat:
            grads = []
            for sample_img, sample_lab in zip(img_real, lab_real):
                sample_out = model(sample_img.unsqueeze(0))
                sample_loss = criterion(sample_out, sample_lab.unsqueeze(0))
                sample_grad = torch.autograd.grad(sample_loss, model.parameters())  # need modify
                sample_grad_flat = torch.cat([gr.data.view(-1) for gr in sample_grad])
                if args.dp_a:
                    clip_coef = max_grad_norm / (sample_grad_flat.data.norm(2) + 1e-7)
                    if clip_coef < 1:
                        sample_grad_flat.mul_(clip_coef)

                grads.append(sample_grad_flat)
            grads = torch.stack(grads).mean(dim=0)

            if args.dp_a:
                noise = torch.randn_like(grads) * noise_multiplier * max_grad_norm
                grads = grads + noise
            #else:
                #print(f'Part A, min: {np.min(stat_a)}, max: {np.max(stat_a)}, median: {np.median(stat_a)},'
                #f'mean: {np.mean(stat_a)}, variance: {np.var(stat_a)}')

            g_real = []
            st, ed = 0, 0
            for param in model.parameters():
                ed = st + torch.numel(param)
                g_real.append(grads[st: ed].reshape(param.shape).detach())
                st = ed

            # noise = torch.randn_like(g) * noise_multiplier
            # stat_a.append(params.detach().norm().item())
        #print(f'Part C, min: {np.min(stat_a)}, max: {np.max(stat_a)}, median: {np.median(stat_a)},'
              #f'mean: {np.mean(stat_a)}, variance: {np.var(stat_a)}')
        else:
            output_real = model(img_real)
            loss_real = criterion(output_real, lab_real)
            g_real = torch.autograd.grad(loss_real, model.parameters())
            g_real = list((g.detach() for g in g_real))

        output_syn = model(img_syn)
        loss_syn = criterion(output_syn, lab_syn)
        g_syn = torch.autograd.grad(loss_syn, model.parameters(), create_graph=True)

        for i in range(len(g_real)):
            if (len(g_real[i].shape) == 1) and not args.bias:  # bias, normliazation
                continue
            if (len(g_real[i].shape) == 2) and not args.fc:
                continue

            loss = add_loss(loss, dist(g_real[i], g_syn[i], method=args.metric))

    if args.stat:
        return loss, grads
    else:
        return loss


def pretrain_sample(args, model, verbose=False):
    """Load pretrained networks
    """
    folder_base = f'./pretrained/{args.datatag}/{args.modeltag}_cut'
    folder_list = glob.glob(f'{folder_base}*')
    tag = np.random.randint(len(folder_list))
    folder = folder_list[tag]

    epoch = args.pt_from
    if args.pt_num > 1:
        epoch = np.random.randint(args.pt_from, args.pt_from + args.pt_num)
    ckpt = f'checkpoint{epoch}.pth.tar'

    file_dir = os.path.join(folder, ckpt)
    load_ckpt(model, file_dir, verbose=verbose)


def condense(args, logger, device='cuda'):
    """Optimize condensed data
    """
    # Define real dataset and loader
    if args.dp_a or args.dp_b:
        sigma = get_noise_multiplier(target_epsilon=args.epsilon, target_delta=args.delta,
                             sample_rate=args.sample_rate,
                             steps=args.dp_steps)
        print('DP steps: ', args.dp_steps)
        print('Noise sigma: ', sigma)

        if args.dp_a:
            if args.dp_b:
                assert args.sigma_a and args.sigma_b, print("Currently does not support noise calculation in mixed mode, "
                                                            "so they must be assigned as command line arguments")
            else:
                if not args.sigma_a:
                    args.sigma_a = sigma
        else:
            if args.dp_b:
                if not args.sigma_b:
                    args.sigma_b = sigma

    args.grad_accu_steps = 1
    if args.dp_b or (args.dp_a and not args.dp_a_org) or args.stat:
        # Set batch-size of real data to 1, and use gradient accumulation instead.
        args.grad_accu_steps = args.batch_real
        args.batch_real = 1

    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    trainset, val_loader = load_resized_data(args)
    images_all = []
    labels_all = []
    images_all = [torch.unsqueeze(trainset[i][0], dim=0) for i in range(len(trainset))]
    labels_all = [trainset[i][1] for i in range(len(trainset))]
    
    images_all = torch.cat(images_all, dim=0).to(device)
    labels_all = torch.tensor(labels_all, dtype=torch.long, device=device)

    dataset = Data(images_all, labels_all)
    def get_init_images(c,n):
    
        query_idxs= strategy_init.query(c,n)

        return images_all[query_idxs]
    if args.load_memory:
        loader_real = ClassMemDataLoader(trainset, batch_size=args.batch_real)
    else:
        loader_real = ClassDataLoader(trainset,
                                      batch_size=args.batch_real,
                                      num_workers=args.workers,
                                      shuffle=True,
                                      pin_memory=True,
                                      drop_last=True)
    nclass = trainset.nclass
    nch, hs, ws = trainset[0][0].shape

    # Define syn dataset
    synset = Synthesizer(args, nclass, nch, hs, ws)

    model = define_model(args, nclass).to(device)
    model.train()
    optim_net = optim.SGD(model.parameters(),
                        args.lr,
                        momentum=args.momentum,
                        weight_decay=args.weight_decay)
    criterion = nn.CrossEntropyLoss()
    aug, aug_rand = diffaug(args)
    '''If ResNet is used to extract features, training in advance is required'''
    # for _ in range(10):
    #     train_epoch(args,
    #                 loader_real,
    #                 model,
    #                 criterion,
    #                 optim_net,
    #                 aug=aug_rand,
    #                 mixup=args.mixup_net)
    strategy_init = get_strategy('KMeansSampling')(dataset, model)
    if args.init == 'kmean':
        print("Kmean initialize synset")
        for c in range(synset.nclass):
            synset.data.data[c*synset.ipc:(c+1)*synset.ipc] = get_init_images(c, synset.ipc).detach().data
    elif args.init == 'random':
        print("Random initialize synset")
        for c in range(synset.nclass):
            img, _ = loader_real.class_sample(c, synset.ipc)
            synset.data.data[synset.ipc * c:synset.ipc * (c + 1)] = img.data.to(synset.device)
    elif args.init == 'mix':
        print("Mixed initialize synset")
        for c in range(synset.nclass):
            if args.f2_init=='random':
                img, _ = loader_real.class_sample(c, synset.ipc * synset.factor**2)
                img = img.data.to(synset.device)
            else:
                img = get_init_images(c, synset.ipc * synset.factor**2).detach()
                img = img.data.to(synset.device)

            s = synset.size[0] // synset.factor
            remained = synset.size[0] % synset.factor
            k = 0
            n = synset.ipc

            h_loc = 0
            for i in range(synset.factor):
                h_r = s + 1 if i < remained else s
                w_loc = 0
                for j in range(synset.factor):
                    w_r = s + 1 if j < remained else s
                    img_part = F.interpolate(img[k * n:(k + 1) * n], size=(h_r, w_r))
                    synset.data.data[n * c:n * (c + 1), :, h_loc:h_loc + h_r,
                                    w_loc:w_loc + w_r] = img_part
                    w_loc += w_r
                    k += 1
                h_loc += h_r

    elif args.init == 'noise':
        pass
    
    query_list=torch.tensor(np.ones(shape=(nclass,args.batch_real)), dtype=torch.long, requires_grad=False, device=device)
    print("init_size:",synset.data.size())
    save_img(os.path.join(args.save_dir, 'init.png'),
             synset.data,
             unnormalize=False,
             dataname=args.dataset)

    # Define augmentation function
    
    save_img(os.path.join(args.save_dir, f'aug.png'),
             aug(synset.sample(0, max_size=args.batch_syn_max)[0]),
             unnormalize=True,
             dataname=args.dataset)
    print("condense begin")
    if not args.test:
        synset.test(args, val_loader, logger, bench=False)
    
    # Data distillation
    optim_img = torch.optim.SGD(synset.parameters(), lr=args.lr_img, momentum=args.mom_img)

    ts = utils.TimeStamp(args.time)
    n_iter = args.niter * 100 // args.inner_loop
    it_log = n_iter // 200
    it_test = np.arange(0, n_iter+1, 10).tolist()

    logger(f"\nStart condensing with {args.match} matching for {n_iter} iteration")
    args.fix_iter = max(1, args.fix_iter)
    grads_accumulator = [[torch.zeros_like(param) for param in synset.parameters()] for c in range(nclass)]
    for it in range(n_iter):
        if it % args.fix_iter == 0 and it != 0:
            model = define_model(args, nclass).to(device)
            model.train()
            optim_net = optim.SGD(model.parameters(),
                                  args.lr,
                                  momentum=args.momentum,
                                  weight_decay=args.weight_decay)
            criterion = nn.CrossEntropyLoss()

            if args.pt_from >= 0:
                pretrain_sample(args, model)
            if args.early > 0:
                for _ in range(args.early):
                    train_epoch(args,
                                loader_real,
                                model,
                                criterion,
                                optim_net,
                                aug=aug_rand,
                                mixup=args.mixup_net)
        
        
        loss_total = 0
        
        synset.data.data = torch.clamp(synset.data.data, min=0., max=1.)
        #print('Gradient Part C batch_size: ', args.batch_real)
        #print('Gradient Part C num_iter: ', args.inner_loop * n_iter)
        stat_a_ls = []
        stat_b_ls = []
        for ot in range(args.inner_loop):
            step = it * args.inner_loop + ot
            ts.set()
            # Update synset
            for c in range(nclass):
                if ot % args.interval == 0:
                    strategy = get_strategy('KMeansSampling')(dataset, model)
                    query_index = strategy.query_match_sample(c,args.batch_real)
                    query_list[c] = query_index
                img = images_all[query_list[c]]
                assert img.size(0) == args.batch_real
                lab = torch.tensor([np.ones(img.size(0))*c], dtype=torch.long, requires_grad=False, device=device).view(-1)
                img_syn, lab_syn = synset.sample(c, max_size=args.batch_syn_max)
                ts.stamp("data")
                n = img.shape[0]
                img_aug = aug(torch.cat([img, img_syn]))
                ts.stamp("aug")
                #print('Gradient Part B batch_size: ', lab_syn.shape)
                #print('Gradient Part B num_iter: ', args.inner_loop * nclass)
                if args.stat:
                    loss, grads = matchloss(args, img_aug[:n], img_aug[n:], lab, lab_syn, model)
                    stat_a_ls.append(grads.detach().norm().item())
                else:
                    loss = matchloss(args, img_aug[:n],  img_aug[n:], lab, lab_syn, model)
                loss_total += loss.item()
                ts.stamp("loss")
                # optim_img.zero_grad()
                loss.backward()

                #if args.dp_b:

                """
                clip_coef = args.max_grad_norm / (synset.data.grad.data.norm(2) + 1e-7)
                if clip_coef < 1:
                    synset.data.grad.mul_(clip_coef)
                synset.data.grad.add_(torch.randn_like(synset.data) * noise_multiplier) * max_grad_norm"""
                if args.dp_b or (args.dp_a and not args.dp_a_org) or args.stat:
                    with torch.no_grad():
                        flatten_gd = torch.cat([g.grad.detach().clone().view(-1) for g in synset.parameters()])
                        stat_b_ls.append(flatten_gd.detach().norm().item())
                        if args.dp_b:
                            clip_coef = args.max_grad_norm_b / (flatten_gd.data.norm(2) + 1e-7)
                            if clip_coef < 1:
                                for gd in synset.parameters():
                                    gd.grad.mul_(clip_coef)
                        for param, grad_accum in zip(synset.parameters(), grads_accumulator[c]):
                            grad_accum.add_(param.grad.detach().clone() / args.grad_accu_steps)
                    if (step + 1) % args.grad_accu_steps == 0:
                        noise = torch.randn_like(flatten_gd) * args.sigma_b * args.max_grad_norm_b
                        st = ed = 0
                        with torch.no_grad():
                            for param, grad_accum in zip(synset.parameters(), grads_accumulator[c]):
                                ed = st + torch.numel(param)
                                param.grad = grad_accum
                                if args.dp_b:
                                    param.grad.add_(noise[st: ed].reshape(param.shape))
                                st = ed
                        optim_img.step()
                        grads_accumulator = [[torch.zeros_like(param) for param in synset.parameters()] for c in range(nclass)]

                else:
                    optim_img.step()
                optim_img.zero_grad()
                ts.stamp("backward")
            #print(
                #f'Part B, min: {np.min(hypergrad_ls)}, max: {np.max(hypergrad_ls)}, median: {np.median(hypergrad_ls)},'
                #f'mean: {np.mean(hypergrad_ls)}, variance: {np.var(hypergrad_ls)}')
            # Net update
            if (step + 1) % args.grad_accu_steps == 0 and args.n_data > 0:
                for _ in range(args.net_epoch):
                    train_epoch(args,
                                loader_real,
                                model,
                                criterion,
                                optim_net,
                                n_data=args.n_data,
                                aug=aug_rand,
                                mixup=args.mixup_net)
            ts.stamp("net update")

            if (ot + 1) % 10 == 0:
                ts.flush()
        if args.stat:
            print(f'Part A, min: {np.min(stat_a_ls)}, max: {np.max(stat_a_ls)}, median: {np.median(stat_a_ls)},'
            f'mean: {np.mean(stat_a_ls)}, variance: {np.var(stat_a_ls)}')

            print(
                f'Part B, min: {np.min(stat_b_ls)}, max: {np.max(stat_b_ls)}, median: {np.median(stat_b_ls)},'
                f'mean: {np.mean(stat_b_ls)}, variance: {np.var(stat_b_ls)}')

        hypergrad_ls = []
        # Logging
        if it % it_log == 0:
            logger(
                f"{utils.get_time()} (Iter {it:3d}) loss: {loss_total/nclass/args.inner_loop:.1f}")
            
        if (it + 1) in it_test:
            save_img(os.path.join(args.save_dir, f'img{it+1}.png'),
                     synset.data,
                     unnormalize=False,
                     dataname=args.dataset)

            # It is okay to clamp data to [0, 1] at here.
            # synset.data.data = torch.clamp(synset.data.data, min=0., max=1.)
            torch.save(
                [synset.data.detach().cpu(), synset.targets.cpu()],
                os.path.join(args.save_dir, f'data{it+1}.pt'))
            print("img and data saved!")

            if not args.test:
                synset.test(args, val_loader, logger)

if __name__ == '__main__':
    import shutil
    from misc.utils import Logger
    from argument import args
    import torch.backends.cudnn as cudnn
    import json

    assert args.ipc > 0

    cudnn.benchmark = True
    if args.seed > 0:
        np.random.seed(args.seed)
        torch.manual_seed(args.seed)
        torch.cuda.manual_seed(args.seed)

    os.makedirs(args.save_dir, exist_ok=True)
    cur_file = os.path.join(os.getcwd(), __file__)
    shutil.copy(cur_file, args.save_dir)

    logger = Logger(args.save_dir)
    logger(f"Save dir: {args.save_dir}")
    with open(os.path.join(args.save_dir, 'args.txt'), 'w') as f:
        json.dump(args.__dict__, f, indent=2)

    condense(args, logger)
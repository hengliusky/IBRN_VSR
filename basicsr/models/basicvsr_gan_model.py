import logging
from copy import deepcopy
import os.path as osp
import importlib
from tqdm import tqdm
import torch
from torch.nn.parallel import DistributedDataParallel
from .sr_gan_model import SRGanModel
from ..utils import imwrite, tensor2img
from ..utils.dist_util import get_dist_info

metric_module = importlib.import_module('basicsr.metrics')

logger = logging.getLogger('basicsr')


class BasicVSRGanModel(SRGanModel):
    """BasicVSR Gan Model.
    """

    def __init__(self, opt):
        super(BasicVSRGanModel, self).__init__(opt)

        if self.is_train:
            self.fix_iter = opt['train'].get('fix_iter')
            if isinstance(self.net_g, DistributedDataParallel):
                logger.warning('Set net_g.find_unused_parameters = True.')
                self.net_g.find_unused_parameters = True

    def setup_optimizers(self):
        train_opt = self.opt['train']
        spynet_lr_mul = train_opt.get('spynet_lr_mul', 1)
        logger.info('Multiple the learning rate '
                    f'for spynet with {spynet_lr_mul}.')
        if spynet_lr_mul == 1:
            optim_params = self.net_g.parameters()
        else:  # separate dcn params and normal params for differnet lr
            normal_params = []
            spynet_params = []
            for name, param in self.net_g.named_parameters():
                if 'spynet' in name:
                    spynet_params.append(param)
                else:
                    normal_params.append(param)
            optim_params = [
                {  # add normal params first
                    'params': normal_params,
                    'lr': train_opt['optim_g']['lr']
                },
                {
                    'params': spynet_params,
                    'lr': train_opt['optim_g']['lr'] * spynet_lr_mul
                },
            ]

        optim_type = train_opt['optim_g'].pop('type')
        if optim_type == 'Adam':
            self.optimizer_g = torch.optim.Adam(optim_params,
                                                **train_opt['optim_g'])
            self.optimizer_d = torch.optim.Adam(self.net_d.parameters(),
                                                **train_opt['optim_g'])
        else:
            raise NotImplementedError(
                f'optimizer {optim_type} is not supperted yet.')
        self.optimizers.append(self.optimizer_g)
        self.optimizers.append(self.optimizer_d)

    def optimize_parameters(self, current_iter):
        if self.fix_iter:
            if current_iter == 1:
                for k, v in self.net_g.named_parameters():
                    if 'spynet' in k:
                        v.requires_grad = False
            elif current_iter == self.fix_iter + 1:
                for v in self.net_g.parameters():
                    v.requires_grad = True

        super(BasicVSRGanModel, self).optimize_parameters(current_iter)

    def dist_validation(self, dataloader, current_iter, tb_logger, save_img):
        # dist_validation has not implemented yet, use nondist_validation
        rank, world_size = get_dist_info()
        if rank == 0:
            self.nondist_validation(dataloader, current_iter, tb_logger,
                                    save_img)

    def nondist_validation(self, dataloader, current_iter, tb_logger,
                           save_img):
        dataset_name = dataloader.dataset.opt['name']
        with_metrics = self.opt['val'].get('metrics') is not None
        if with_metrics:
            self.metric_results = {
                metric: 0
                for metric in self.opt['val']['metrics'].keys()
            }

        pbar = tqdm(total=len(dataloader), unit='image', ascii=True)

        # print("dataloader length is: {}".format(len(dataloader)))

        # idx = 0

        for idx, val_data in enumerate(dataloader):
            # val_data['key'] = val_data['key'][0]
            # clip_name = val_data['key'].split('/')[0]
            clip_name = val_data['key'][0]
            self.feed_data(val_data)
            self.test()

            visuals = self.get_current_visuals(dataset_name)
            sr_imgs = tensor2img(visuals['result'])
            if 'gt' in visuals:
                gt_imgs = tensor2img(visuals['gt'])
                del self.gt

            # tentative for out of GPU memory
            del self.lq
            del self.output
            torch.cuda.empty_cache()

            if save_img:
                if self.opt['is_train']:
                    save_img_name = osp.join(self.opt['path']['visualization'],
                                             f'{dataset_name}_train',
                                             clip_name, '{idx:08d}.png')
                else:
                    if self.opt['val']['suffix']:
                        save_img_name = osp.join(
                            self.opt['path']['visualization'], dataset_name,
                            clip_name, ('{idx:08d}_' +
                                        f'{self.opt["val"]["suffix"]}.png'))
                    else:
                        save_img_name = osp.join(
                            self.opt['path']['visualization'], dataset_name,
                            clip_name, '{idx:08d}_.png')
                            
                for sr_img_idx, sr_img in zip(val_data['key'], sr_imgs):
                    imwrite(sr_img, save_img_name.format(idx=sr_img_idx.item()))

            if with_metrics:
                # calculate metrics
                opt_metric = deepcopy(self.opt['val']['metrics'])
                for name, opt_ in opt_metric.items():
                    metric_type = opt_.pop('type')
                    metric_ = getattr(metric_module, metric_type)
                    metric_results_ = [
                        metric_(sr, gt, **opt_)
                        for sr, gt in zip(sr_imgs, gt_imgs)
                    ]
                    self.metric_results[name] += torch.tensor(
                        sum(metric_results_) / len(metric_results_))
            pbar.update(1)
            pbar.set_description(f'Test {clip_name}')
        pbar.close()

        if with_metrics:
            for metric in self.metric_results.keys():
                self.metric_results[metric] /= (idx + 1)

            super(VideoBaseModel,
                self)._log_validation_metric_values(current_iter,
                                                    dataset_name, tb_logger)

    def get_current_visuals(self, dataset_name):
        # dim: n,t,c,h,w
        t = self.lq.shape[1]
        
        lq = self.lq.detach().cpu().squeeze(0)
        gt = self.gt.detach().cpu().squeeze(0)
        result = self.output.detach().cpu().squeeze(0)

        if dataset_name == 'Vimeo90K':
            # dim:
            #   lq: n,t,c,h,w
            #   gt: n,c,h,w
            return dict(
                lq=[lq[3]],
                gt=[gt],
                result=[result[3]])
        else:
            assert (t == self.gt.shape[1] and t == self.output.shape[1])

        return dict(
            lq=[lq[i] for i in range(t)],
            gt=[gt[i] for i in range(t)],
            result=[result[i] for i in range(t)])

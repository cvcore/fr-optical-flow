import argparse
import os
import time

import torch
import torch.nn.functional as F
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim
import torch.utils.data
import torchvision.transforms as transforms
import flow_transforms
import models
import datasets
from multiscaleloss import multiscaleEPE, realEPE
from own_loss import *
import datetime
from tensorboardX import SummaryWriter
import wandb
from util import flow2rgb, AverageMeter, save_checkpoint, save_image


model_names = sorted(name for name in models.__dict__
                     if name.islower() and not name.startswith("__"))
dataset_names = sorted(name for name in datasets.__all__)

parser = argparse.ArgumentParser(description='PyTorch FlowNet Training on several datasets',
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
parser.add_argument('data', metavar='DIR',
                    help='path to dataset')
parser.add_argument('--dataset', metavar='DATASET', default='flying_chairs',
                    choices=dataset_names,
                    help='dataset type : ' +
                    ' | '.join(dataset_names))
group = parser.add_mutually_exclusive_group()
group.add_argument('-s', '--split-file', default=None, type=str,
                   help='test-val split file')
group.add_argument('--split-value', default=0.8, type=float,
                   help='test-val split proportion between 0 (only test) and 1 (only train), '
                        'will be overwritten if a split file is set')
parser.add_argument('--arch', '-a', metavar='ARCH', default='flownets',
                    choices=model_names,
                    help='model architecture, overwritten if pretrained is specified: ' +
                    ' | '.join(model_names))
parser.add_argument('--solver', default='adam',choices=['adam','sgd'],
                    help='solver algorithms')
parser.add_argument('-j', '--workers', default=8, type=int, metavar='N',
                    help='number of data loading workers')
# parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
#                     help='manual epoch number (useful on restarts)')
parser.add_argument('--epoch-size', default=1000, type=int, metavar='N',
                    help='manual epoch size (will match dataset size if set to 0)')
parser.add_argument('-b', '--batch-size', default=8, type=int,
                    metavar='N', help='mini-batch size')
parser.add_argument('--lr', '--learning-rate', default=0.0001, type=float,
                    metavar='LR', help='initial learning rate')
parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                    help='momentum for sgd, alpha parameter for adam')
parser.add_argument('--beta', default=0.999, type=float, metavar='M',
                    help='beta parameter for adam')
parser.add_argument('--weight-decay', '--wd', default=4e-4, type=float,
                    metavar='W', help='weight decay')
parser.add_argument('--bias-decay', default=0, type=float,
                    metavar='B', help='bias decay')
parser.add_argument('--multiscale-weights', '-w', default=[0.005,0.01,0.02,0.08,0.32], type=float, nargs=5,
                    help='training weight for each scale, from highest resolution (flow2) to lowest (flow6)',
                    metavar=('W2', 'W3', 'W4', 'W5', 'W6'))
parser.add_argument('--sparse', action='store_true',
                    help='look for NaNs in target flow when computing EPE, avoid if flow is garantied to be dense,'
                    'automatically seleted when choosing a KITTIdataset')
parser.add_argument('--print-freq', '-p', default=200, type=int,
                    metavar='N', help='print frequency')
parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true',
                    help='evaluate model on validation set')
parser.add_argument('--pretrained', dest='pretrained', default=None,
                    help='path to pre-trained model')
parser.add_argument('--no-date', action='store_true',
                    help='don\'t append date timestamp to folder' )
parser.add_argument('--div-flow', default=20, help='value by which flow will be divided. Original value is 20 but 1 with batchNorm gives good results')
parser.add_argument('--milestones', default=[100,150,200], metavar='N', nargs='*', help='epochs at which learning rate is divided by 2')
parser.add_argument('--self-supervised-loss', default=True, help='use self-supervised loss (photometric and smoothness)')
parser.add_argument('--device', type=str, default=None)
parser.add_argument('--unflow', default=True, help='use of ternary and second order losses from Unflow paper)')

args = parser.parse_args()

best_EPE = -1
n_iter = 0

if args.device is None:
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
else:
    device = torch.device(args.device)


def get_default_config():
    cfg = {}
    cfg["sl_weight"] = 0.002
    cfg["pl_weight"] = 1
    cfg["fb_weight"] = 1
    cfg["sl_exp"] = 0.38
    cfg["pl_exp"] = 0.25
    cfg["fb_exp"] = 0.45
    cfg["weighted_sl_loss"] = True
    cfg["epochs"] = 1000
    cfg["multiscale_sl_loss"] = True
    cfg["multiscale_pl_loss"] = True
    cfg["multiscale_census_loss"] = True
    cfg["multiscale_ssim_loss"] = True
    cfg["multiscale_fb_loss"] = True
    cfg["use_l1_loss"] = False
    cfg["unflow"] = True
    cfg["sl"] = True
    cfg["census"] = True
    cfg["ssim"] = False
    cfg["fb"] = False


    return cfg


def main(config=get_default_config()):
    global best_EPE

    wandb.init(project="fr-optical-flow", sync_tensorboard=True)
    wandb.config.update(args) # log configs passed in from progrom arguments
    wandb.config.update(config) # log also configs coming from BOHB interface

    save_path = '{},{},{},b{},lr{}'.format(
        args.arch,
        args.solver,
        ',epochSize'+str(args.epoch_size) if args.epoch_size > 0 else '',
        args.batch_size,
        args.lr)
    if not args.no_date:
        timestamp = datetime.datetime.now().strftime("%m-%d-%H:%M")
        save_path = os.path.join(timestamp,save_path)
    save_path = os.path.join(args.dataset,save_path)
    print('=> will save everything to {}'.format(save_path))
    if not os.path.exists(save_path):
        os.makedirs(save_path)

    train_writer = SummaryWriter(os.path.join(save_path,'train'))
    test_writer = SummaryWriter(os.path.join(save_path,'test'))
    output_writers = []
    for i in range(3):
        output_writers.append(SummaryWriter(os.path.join(save_path,'test',str(i))))

    # Data loading code
    input_transform = transforms.Compose([
        flow_transforms.ArrayToTensor(),
        transforms.Normalize(mean=[0,0,0], std=[255,255,255]),
        transforms.Normalize(mean=[0.45,0.432,0.411], std=[1,1,1])
    ])
    target_transform = transforms.Compose([
        flow_transforms.ArrayToTensor(),
        transforms.Normalize(mean=[0,0],std=[args.div_flow,args.div_flow])
    ])

    if 'KITTI' in args.dataset:
        args.sparse = True
    if args.sparse:
        co_transform = flow_transforms.Compose([
            flow_transforms.RandomCrop((320,448)),
            flow_transforms.RandomVerticalFlip(),
            flow_transforms.RandomHorizontalFlip()
        ])
    elif args.arch == 'pwcnet':
        co_transform = flow_transforms.Compose([
            flow_transforms.RandomTranslate(10),
            flow_transforms.RandomRotate(10,5),
            flow_transforms.RandomCrop((320,448)),
            flow_transforms.RandomVerticalFlip(),
            flow_transforms.RandomHorizontalFlip()
        ])
    else:
        co_transform = flow_transforms.Compose([
            flow_transforms.RandomTranslate(10),
            flow_transforms.RandomRotate(10,5),
            flow_transforms.RandomCrop((320,448)),
            flow_transforms.RandomVerticalFlip(),
            flow_transforms.RandomHorizontalFlip()
        ])

    print("=> fetching img pairs in '{}'".format(args.data))
    train_set, test_set = datasets.__dict__[args.dataset](
        args.data,
        transform=input_transform,
        target_transform=target_transform,
        co_transform=co_transform,
        split=args.split_file if args.split_file else args.split_value
    )
    print('{} samples found, {} train samples and {} test samples '.format(len(test_set)+len(train_set),
                                                                           len(train_set),
                                                                           len(test_set)))
    train_loader = torch.utils.data.DataLoader(
        train_set, batch_size=args.batch_size,
        num_workers=args.workers, pin_memory=True, shuffle=True)
    val_loader = torch.utils.data.DataLoader(
        test_set, batch_size=args.batch_size,
        num_workers=args.workers, pin_memory=True, shuffle=False)

    # create model
    if args.pretrained:
        network_data = torch.load(args.pretrained)
        args.arch = network_data['arch']
        print("=> using pre-trained model '{}'".format(args.arch))
    else:
        network_data = None
        print("=> creating model '{}'".format(args.arch))

    model = models.__dict__[args.arch](network_data).to(device)
    # model = torch.nn.DataParallel(model).cuda()
    cudnn.benchmark = True

    assert(args.solver in ['adam', 'sgd'])
    print('=> setting {} solver'.format(args.solver))
    param_groups = [{'params': model.bias_parameters(), 'weight_decay': args.bias_decay},
                    {'params': model.weight_parameters(), 'weight_decay': args.weight_decay}]
    if args.solver == 'adam':
        optimizer = torch.optim.Adam(param_groups, args.lr,
                                     betas=(args.momentum, args.beta))
    elif args.solver == 'sgd':
        optimizer = torch.optim.SGD(param_groups, args.lr,
                                    momentum=args.momentum)

    if args.evaluate:
        best_EPE = validate(val_loader, model, 0, output_writers)
        return

    scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=args.milestones, gamma=0.5)
    wandb.watch(model, log='all')

    #for epoch in range(args.start_epoch, args.epochs):
    for epoch in range(int(config["epochs"])):

        # train for one epoch
        train_loss, train_EPE = train(train_loader, model, optimizer, epoch, train_writer, config)
        scheduler.step()
        train_writer.add_scalar('mean EPE', train_EPE, epoch)

        # evaluate on validation set

        with torch.no_grad():
            EPE = validate(val_loader, model, epoch, output_writers)
        test_writer.add_scalar('mean EPE', EPE, epoch)

        if best_EPE < 0:
            best_EPE = EPE

        is_best = EPE < best_EPE
        best_EPE = min(EPE, best_EPE)
        save_checkpoint({
            'epoch': epoch + 1,
            'arch': args.arch,
            'state_dict': model.state_dict(),
            'best_EPE': best_EPE,
            'div_flow': args.div_flow
        }, is_best, save_path)

    return best_EPE


def train(train_loader, model, optimizer, epoch, train_writer, config):
    global n_iter

    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    flow2_EPEs = AverageMeter()

    epoch_size = len(train_loader) if args.epoch_size == 0 else min(len(train_loader), args.epoch_size)

    # switch to train mode
    model.train()

    end = time.time()

    if not args.self_supervised_loss:
        # use old loss
        for i, (input, target) in enumerate(train_loader):
            # measure data loading time
            data_time.update(time.time() - end)
            target = target.to(device)
            input = torch.cat(input,1).to(device)

            # compute output
            output = model(input)
            if args.sparse:
                # Since Target pooling is not very precise when sparse,
                # take the highest resolution prediction and upsample it instead of downsampling target
                h, w = target.size()[-2:]
                output = [F.interpolate(output[0], (h,w)), *output[1:]]

            loss = multiscaleEPE(output, target, weights=args.multiscale_weights, sparse=args.sparse)
            flow2_EPE = args.div_flow * realEPE(output[0], target, sparse=args.sparse)
            # record loss and EPE
            losses.update(loss.item(), target.size(0))
            train_writer.add_scalar('train_loss', loss.item(), n_iter)
            flow2_EPEs.update(flow2_EPE.item(), target.size(0))

            # compute gradient and do optimization step
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            # measure elapsed time
            batch_time.update(time.time() - end)
            end = time.time()

            if i % args.print_freq == 0:
                print('Epoch: [{0}][{1}/{2}]\t Time {3}\t Data {4}\t Loss {5}\t EPE {6}'
                      .format(epoch, i, epoch_size, batch_time,
                              data_time, losses, flow2_EPEs))
            n_iter += 1
            if i >= epoch_size:
                break

        return losses.avg, flow2_EPEs.avg
    elif args.unflow:
        weights = [0.005, 0.01, 0.02, 0.08, 0.32]

        for it, (input, target) in enumerate(train_loader):
            # measure data loading time
            data_time.update(time.time() - end)
            target = target.to(device)
            im1 = input[0].to(device)
            im2 = input[1].to(device)
            input_fw = torch.cat(input, 1).to(device)
            pred_fw = model(input_fw)
            input_bw = torch.cat((im2, im1), 1).to(device)
            pred_bw = model(input_bw)



            census_loss = 0
            census_loss_list = []
            if config['census']:
                #weights = [1, 0.34, 0.31, 0.27, 0.09]
                #max_dist = [3, 2, 2, 1, 1]
                for i in range(len(pred_fw)):
                    flow_fw = pred_fw[i] * args.div_flow
                    flow_bw = pred_bw[i] * args.div_flow
                    loss = ternary_loss(im2, im1,  flow_fw, max_distance=1) +\
                        ternary_loss(im1, im2, flow_bw,max_distance=1)
                    census_loss += loss
                    census_loss_list.append(loss.item())
                    if not config['multiscale_census_loss']:
                        break
                train_writer.add_scalar('train_loss_census', census_loss.item(), n_iter)

            sl_loss = 0
            sl_loss_list = []
            if config['sl']:
                for i in range(len(pred_fw)):
                    flow_fw = pred_fw[i] * args.div_flow
                    flow_bw = pred_bw[i] * args.div_flow
                    loss = smoothness_loss(flow_fw,config) + smoothness_loss(flow_bw,config)
                    #loss = smoothness_loss(flow_bw, config)
                    sl_loss += loss
                    sl_loss_list.append(loss.item())
                    if not config['multiscale_sl_loss']:
                        break
                train_writer.add_scalar('train_loss_sl', sl_loss.item(), n_iter)

            ssim_loss = 0
            ssim_loss_list = []
            if config['ssim']:
                for i in range(len(pred_bw)):
                    flow_bw = pred_bw[i] * args.div_flow
                    loss = ssim(im1,im2,flow_bw)
                    ssim_loss += loss
                    ssim_loss_list.append(loss.item())
                    if not config['multiscale_ssim_loss']:
                        break
                train_writer.add_scalar('train_loss_ssim', ssim_loss.item(), n_iter)

            fb_loss = 0
            fb_loss_list = []
            if config['fb']:
                for i in range(len(pred_bw)):
                    flow_fw = pred_fw[i] * args.div_flow
                    flow_bw = pred_bw[i] * args.div_flow
                    loss = forward_backward_loss(im1=im1, im2=im2, flow_fw=flow_fw, flow_bw=flow_bw, config=config)
                    fb_loss += loss
                    fb_loss_list.append(loss.item())
                    if not config['multiscale_fb_loss']:
                        break
                train_writer.add_scalar('train_loss_fb', fb_loss.item(), n_iter)

            # to check the magnitude of both losses
            if it % 500 == 0:
                print("[DEBUG] census_loss:", str(census_loss_list))
                print("[DEBUG] sl_loss:", str(sl_loss_list))
                print("[DEBUG] ssim_loss:", str(ssim_loss_list))
                print("[DEBUG] fb_loss:", str(fb_loss_list))

            loss = census_loss + sl_loss + ssim_loss + 0.001*fb_loss

            # record loss and EPE
            flow = pred_bw[0]
            losses.update(loss.item(), target.size(0))
            flow2_EPE = args.div_flow * realEPE(flow, target, sparse=args.sparse)
            train_writer.add_scalar('train_loss', loss.item(), n_iter)
            flow2_EPEs.update(flow2_EPE.item(), target.size(0))

            # compute gradient and do optimization step
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            # measure elapsed time
            batch_time.update(time.time() - end)
            end = time.time()

            if it % args.print_freq == 0:
                print('Epoch: [{0}][{1}/{2}]\t Time {3}\t Data {4}\t Loss {5}\t EPE {6}'
                      .format(epoch, it, epoch_size, batch_time,
                              data_time, losses, flow2_EPEs))
            n_iter += 1
            if it >= epoch_size:
                break

        return losses.avg, flow2_EPEs.avg


    else:
        # use self-supervised loss
        for it, (input, target) in enumerate(train_loader):
            # measure data loading time
            data_time.update(time.time() - end)
            target = target.to(device)
            im1 = input[0].to(device)
            im2 = input[1].to(device)
            input = torch.cat(input,1).to(device)
            pred = model(input)

            pl_loss = 0
            pl_loss_list = []
            for i in range(len(pred)):
                flow = pred[i] * args.div_flow
                loss = photometric_loss(im1, im2, flow, config)
                pl_loss += loss
                pl_loss_list.append(loss.item())

                if not config['multiscale_pl_loss']:
                    break

            sl_loss = 0
            sl_loss_list = []
            if config['weighted_sl_loss']:
                for i in range(len(pred)):
                    flow = pred[i] * args.div_flow
                    loss = weighted_smoothness_loss(im1, im2, flow, config)
                    sl_loss += loss
                    sl_loss_list.append(loss.item())

                    if not config['multiscale_sl_loss']:
                        break

            else:
                # smoothness loss for multi resolution flow pyramid
                for i in range(len(pred)):
                    flow = pred[i] * args.div_flow
                    loss = smoothness_loss(flow, config)
                    sl_loss += loss
                    sl_loss_list.append(loss.item())

                    if not config['multiscale_sl_loss']:
                        break
            # to check the magnitude of both losses
            if it % 500 == 0:
                print("[DEBUG] pl_loss:", str(pl_loss_list))
                print("[DEBUG] sl_loss:", str(sl_loss_list))

            loss = pl_loss + sl_loss

            # record loss and EPE
            flow = pred[0]
            losses.update(loss.item(), target.size(0))
            flow2_EPE = args.div_flow * realEPE(flow, target, sparse=args.sparse)
            train_writer.add_scalar('train_loss', loss.item(), n_iter)
            train_writer.add_scalar('train_loss_pl', pl_loss.item(), n_iter)
            train_writer.add_scalar('train_loss_sl', sl_loss.item(), n_iter)
            flow2_EPEs.update(flow2_EPE.item(), target.size(0))

            # compute gradient and do optimization step
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            # measure elapsed time
            batch_time.update(time.time() - end)
            end = time.time()

            if it % args.print_freq == 0:
                print('Epoch: [{0}][{1}/{2}]\t Time {3}\t Data {4}\t Loss {5}\t EPE {6}'
                      .format(epoch, it, epoch_size, batch_time,
                              data_time, losses, flow2_EPEs))
            n_iter += 1
            if it >= epoch_size:
                break

        return losses.avg, flow2_EPEs.avg

def validate(val_loader, model, epoch, output_writers):

    batch_time = AverageMeter()
    flow2_EPEs = AverageMeter()

    # switch to evaluate mode
    model.eval()

    end = time.time()
    for i, (input, target) in enumerate(val_loader):
        target = target.to(device)
        input = torch.cat(input,1).to(device)

        # compute output
        output = model(input)
        flow2_EPE = args.div_flow*realEPE(output, target, sparse=args.sparse)
        # record EPE
        flow2_EPEs.update(flow2_EPE.item(), target.size(0))

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i < len(output_writers):  # log first output of first batches
            if epoch == 0:
                mean_values = torch.tensor([0.45,0.432,0.411], dtype=input.dtype).view(3,1,1)
                output_writers[i].add_image('GroundTruth', flow2rgb(args.div_flow * target[0], max_value=10), 0)
                output_writers[i].add_image('Inputs', (input[0,:3].cpu() + mean_values).clamp(0,1), 0)
                output_writers[i].add_image('Inputs', (input[0,3:].cpu() + mean_values).clamp(0,1), 1)
            output_writers[i].add_image('FlowNet Outputs', flow2rgb(args.div_flow * output[0], max_value=10), epoch)

        if i % args.print_freq == 0:
            print('Test: [{0}/{1}]\t Time {2}\t EPE {3}'
                  .format(i, len(val_loader), batch_time, flow2_EPEs))

    print(' * EPE {:.3f}'.format(flow2_EPEs.avg))

    return flow2_EPEs.avg


if __name__ == '__main__':
    main()

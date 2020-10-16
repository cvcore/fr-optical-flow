# Self-supervised Learning of Optical Flow

Collecting groundtruth data for optical flow is hard.

In this research project, we compare and analyze a set of self-supervised losses to train an optical flow network without the groundtruth labels. This approach enables a network to learn optical flow from only pairs of consecutive images or from videos.

The network is able to achieve a validation endpoint error (EPE) of 5.5 on the FlyingChairs dataset, trained with only photometric and smoothness loss. Pretrained weights can be downloaded for evaluation.

## Results

![Evaluation Results](code/images/eval_results.png)

## Setup

To setup, simply clone this repository locally and make sure you have the following packages installed:

- [Anaconda](https://www.anaconda.com)
- [PyTorch](https://pytorch.org), together with torchvision
- [WandB](https://www.wandb.com)
- [Numpy](https://numpy.org)

## Dataset

You can download the dataset for training the FlowNetS network with the script `dataset/download_dataset.sh`.

## Training

To train the model, run

    python code/main.py PATH_DATASET --dataset flying_chairs --arch flownets --device cuda:0

Then, the training log can be seen in `tensorboard` by running:

    tensorboard --logdir flying_chairs/ --host 0.0.0.0

In addition, this script supports training FlownetS and PWCNet with a combination of the following losses:

- Photometric loss
- Smoothness loss
- Forward & backward loss
- Tenary loss
- SSIM loss

Change `get_default_config()` function in `code/main.py` to set weights for each loss.

## Evaluation

Pretrained models

| Best Models                               | Evaluation EPE | Download Link                                                                                     |
| ----------------------------------------- | -------------- | ------------------------------------------------------------------------------------------------- |
| Supervised FlowNetS                       | 2.391          | [Dropbox](https://www.dropbox.com/s/6dt6noqms64wkxp/supervised_flownets_chairs.pth.tar?dl=0)      |
| Self-supervised Phometric + Smoothness    | 5.505          | [Dropbox](https://www.dropbox.com/s/w3vvsi8oyx2mt4f/pl_sl_chairs.pth.tar?dl=0)                    |
| SSIM + Smoothness + Fwd & bwd Consistency | 5.48           | [Google Drive](https://drive.google.com/uc?id=1SSt5bt6CmrXNjSIEPTjfDKhJmk9-dIxP&export=download)  |

For evaluation you can download our pretrained model and run

    python code/run_inference.py PATH_DATASET PATH_MODEL_PTH --output PATH_OUTPUT

Then, the model prediction together with the groundtruth label will be saved in `PATH_OUTPUT` folder.

Please note: for the SSIM model, you need an extra argument `--bidirectional True` for evaluation.

## Reference

If you find this implementation useful in your work, please acknowledge it appropriately:

```
@misc{self-supervised-optical-flow,
  author = {Chengxin Wang and Thomas Nierhoff and Abdelrahman Younes},
  title = {self-supervised-optical-flow: Self-supervised Learning of Optical Flow},
  year = {2020},
  publisher = {GitHub},
  journal = {GitHub repository},
  howpublished = {\url{https://github.com/cvcore/self-supervised-optical-flow}
}
```

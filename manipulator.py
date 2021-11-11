import argparse
import copy
from tqdm import tqdm
from pprint import pprint

import numpy as np
import PIL.Image
import torch

import clip
import dnnlib
import legacy
from wrapper import Generator


class StyleManipulator():
    """Manipulator for style editing

    in paper, use 100 image pairs to estimate the mean for alpha(magnitude of the perturbation) [-5, 5]

    *** Args ***
    G : Genertor wrapper for synthesis styles
    device : torch.device
    lst_alpha : magnitude of the perturbation
    num_images : num images to process

    *** Attributes ***
    S :  List[dict(str, torch.Tensor)] # length 2,000
    styles : List[dict(str, torch.Tensor)] # length of num_images
                (num_images, style)
    lst_alpha : List[int]
    boundary : (num_images, len_alpha)
    edited_styles : List[styles]
    edited_images : List[(num_images, 3, 1024, 1024)]
    """
    def __init__(self, G, device, lst_alpha, num_images):
        self.W = torch.load('tensor/W.pt')
        self.S = torch.load('tensor/S.pt')
        self.S_mean = torch.load('tensor/S_mean.pt')
        self.S_std = torch.load('tensor/S_std.pt')

        self.S = {layer: self.S[layer].to(device) for layer in G.style_layers}
        self.styles = {layer: self.S[layer][:num_images] for layer in G.style_layers}
        self.latent = self.W[:num_images]
        self.latent = self.latent.to(device)
        del self.W
        del self.S
        self.S_mean = {layer: self.S_mean[layer].to(device) for layer in G.style_layers}
        self.S_std = {layer: self.S_std[layer].to(device) for layer in G.style_layers}

        self.G = G
        self.device = device
        self.num_images = num_images
        self.lst_alpha = lst_alpha

    def edit_one_channel(self, layer, channel_ind:int):
        assert layer in self.G.style_layers
        assert 0 <= channel_ind < self.styles[layer].shape[1]
        boundary = self.S_std[layer][channel_ind].item()
        # apply self.S_mean value for given layer, channel_ind
        for ind in range(self.num_images):
            self.styles[layer][ind][channel_ind] = self.S_mean[layer][channel_ind]
        styles = [copy.deepcopy(self.styles) for _ in range(len(self.lst_alpha))]
        
        perturbation = (torch.Tensor(self.lst_alpha) * boundary).numpy().tolist()
       
        # apply edit 
        for img_ind in range(self.num_images):
            for edit_ind, delta_s in enumerate(perturbation):
                styles[edit_ind][layer][img_ind][channel_ind] += delta_s

        return styles

    def synthesis_from_styles(self, styles, start_ind, end_ind):
        styles_ = list()
        for style in styles:
            style_ = dict()
            for layer in self.G.style_layers:
                style_[layer] = style[layer][start_ind:end_ind].to(self.device)
            styles_.append(style_)

        imgs = [self.G.synthesis_from_stylespace(self.latent[start_ind:end_ind], style_).cpu() 
                for style_ in styles_]
        return imgs


def extract_global_direction(G, device, lst_alpha, num_images):
    """Extract global style direction in 100 images
    """
    assert len(lst_alpha) == 2
    model, preprocess = clip.load("ViT-B/32", device=device)
    
    # lindex in original tf version
    manipulate_layers = [layer for layer in G.style_layers if 'torgb' not in layer] 

    # total channel: 6048
    latent = torch.randn([1,18,512]).to(device)
    style = G.mapping_stylespace(latent)
    cnt = 0
    for layer in manipulate_layers:
        cnt += style[layer].shape[1]
    del latent
    del style
    print(f"total channels to manipulate: {cnt}")
    
    manipulator = StyleManipulator(G, device, lst_alpha, num_images)

    all_feats = list()
    for layer in manipulate_layers:
        print(f'\nStyle manipulation in layer "{layer}"')
        channel_num = manipulator.styles[layer].shape[1]
        for channel_ind in tqdm(range(channel_num), total=channel_num):
            styles = manipulator.edit_one_channel(layer, channel_ind)
            # 2 * 100 images
            batchsize = 10
            nbatch = int(100 / batchsize)
            feats = list()
            for img_ind in range(0, nbatch): # batch size 10 * 2
                start = img_ind*nbatch
                end = img_ind*nbatch + batchsize
                synth_imgs = manipulator.synthesis_from_styles(styles, start, end)
                synth_imgs = [(synth_img.permute(0,2,3,1)*127.5+128).clamp(0,255).to(torch.uint8).numpy()
                            for synth_img in synth_imgs]
                imgs = list()
                for i in range(batchsize):
                    img0 = PIL.Image.fromarray(synth_imgs[0][i])
                    img1 = PIL.Image.fromarray(synth_imgs[1][i])
                    imgs.append(preprocess(img0).unsqueeze(0).to(device))
                    imgs.append(preprocess(img1).unsqueeze(0).to(device))
                with torch.no_grad():
                    feat = model.encode_image(torch.cat(imgs))
                feats.append(feat)
            all_feats.append(torch.cat(feats).view([-1, 2, 512]).cpu())

    all_feats = torch.stack(all_feats).numpy()

    fs = all_feats
    fs1=fs/np.linalg.norm(fs,axis=-1)[:,:,:,None]
    fs2=fs1[:,:,1,:]-fs1[:,:,0,:] # 5*sigma - (-5)*sigma
    fs3=fs2/np.linalg.norm(fs2,axis=-1)[:,:,None]
    fs3=fs3.mean(axis=1)
    fs3=fs3/np.linalg.norm(fs3,axis=-1)[:,None]

    np.save('tensor/fs3.npy', fs3)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    parser.add_argument('runtype', type=str, default='test')
    args = parser.parse_args()
    
    runtype = args.runtype

    assert runtype in ['test', 'extract'] 

    device = torch.device('cuda:0')
    ckpt = 'pretrained/ffhq.pkl'
    G = Generator(ckpt, device)

    if runtype == 'test': # test manipulator
        num_images = 100
        lst_alpha = [-5, 0, 5]
        layer = G.style_layers[6]
        channel_ind = 501
        manipulator = StyleManipulator(G, device, lst_alpha, num_images)
        styles = manipulator.edit_one_channel(layer, channel_ind)
        start_ind, end_ind= 0, 10
        imgs = manipulator.synthesis_from_styles(styles, start_ind, end_ind)
        print(len(imgs), imgs[0].shape)

    elif runtype == 'extract': # extract global style direction from "tensor/S.pt"
        num_images = 100
        lst_alpha = [-5, 5]
        extract_global_direction(G, device, lst_alpha, num_images)

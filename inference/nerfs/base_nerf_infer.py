import os
import cv2
import torch
import numpy as np
import importlib
import tqdm

from utils.commons.hparams import hparams, set_hparams
from utils.commons.ckpt_utils import load_ckpt, get_last_checkpoint
from utils.commons.euler2rot import euler_trans_2_c2w, c2w_to_euler_trans
from utils.commons.tensor_utils import move_to_cpu, move_to_cuda

from tasks.nerfs.dataset_utils import NeRFDataset


class BaseNeRFInfer:
    def __init__(self, hparams, device=None):
        if device is None:
            device = 'cuda' if torch.cuda.is_available() else 'cpu'
        self.hparams = hparams
        self.infer_max_length = hparams.get('infer_max_length', 500000) # default render 10 seconds long
        self.device = device
        self.dataset = NeRFDataset(prefix='train') # the dataset only provides head pose
        self.nerf_task = self.build_nerf_task()
        self.nerf_task.eval()
        self.nerf_task.to(self.device)

        
    def build_nerf_task(self):
        assert hparams['task_cls'] != ''
        pkg = ".".join(hparams["task_cls"].split(".")[:-1])
        cls_name = hparams["task_cls"].split(".")[-1]
        task_cls = getattr(importlib.import_module(pkg), cls_name)
        task = task_cls()
        task.build_model()
        task.eval()
        load_ckpt(task.model, hparams['work_dir'], 'model')
        ckpt, _ = get_last_checkpoint(hparams['work_dir'])
        task.global_step = ckpt['global_step']
        return task

    def _forward_nerf_task(self, batches):
        tmp_imgs_dir = self.inp['tmp_imgs_dir']
        os.makedirs(tmp_imgs_dir, exist_ok=True)
        H, W = batches[0]['H'], batches[0]['W']
        with torch.no_grad():
            for idx, batch in tqdm.tqdm(enumerate(batches), total=len(batches),
                                desc=f"Now NeRF is rendering the images into {tmp_imgs_dir}"):
                torch.cuda.empty_cache()
                if self.device == 'cuda':
                    batch = move_to_cuda(batch)
                model_out = self.nerf_task.run_model(batch, infer=True)
                pred_rgb = model_out['rgb_map'] * 255
                pred_img = pred_rgb.view([H, W, 3]).cpu().numpy().astype(np.uint8)
                out_name = os.path.join(tmp_imgs_dir, format(idx, '05d')+".png")
                bgr_img = cv2.cvtColor(pred_img, cv2.COLOR_RGB2BGR)
                cv2.imwrite(out_name, bgr_img)
                batches[idx] = move_to_cpu(batch)
                for k in list(batch.keys()):
                    del batch[k]
                torch.cuda.empty_cache()
        return tmp_imgs_dir
    
    def forward_system(self, batches):
        img_dir = self._forward_nerf_task(batches)
        return img_dir

    def get_cond_from_input(self, inp):
        """
        get the conditon features of NeRF
        """
        raise NotImplementedError

    def get_pose_from_ds(self, samples):
        """
        process the item into torch.tensor batch
        """
        for idx, sample in enumerate(samples):
            if idx >= len(self.dataset.samples):
                del samples[idx:]
                break
            sample['H'] = self.dataset.H
            sample['W'] = self.dataset.W
            sample['focal'] = self.dataset.focal
            sample['cx'] = self.dataset.cx
            sample['cy'] = self.dataset.cy
            sample['near'] = hparams['near']
            sample['far'] = hparams['far']
            sample['bc_img'] = self.dataset.bc_img

            sample['c2w'] = self.dataset.samples[idx]['c2w'][:3]
            sample['c2w_t0'] = self.dataset.samples[0]['c2w'][:3]

            sample['t'] = torch.tensor([0,]).float()
            euler, trans = c2w_to_euler_trans(sample['c2w'])
            euler_t0, trans_t0 = c2w_to_euler_trans(sample['c2w_t0'])
            sample['euler'] = torch.tensor(np.ascontiguousarray(euler)).float()
            sample['trans'] = torch.tensor(np.ascontiguousarray(trans)).float()
            sample['euler_t0'] = torch.tensor(np.ascontiguousarray(euler_t0)).float()
            sample['trans_t0'] = torch.tensor(np.ascontiguousarray(trans_t0)).float()
        return samples

    def postprocess_output(self, output):
        tmp_imgs_dir = self.inp['tmp_imgs_dir']
        out_video_name = self.inp['out_video_name']
        self.save_mp4(tmp_imgs_dir, self.wav16k_name, out_video_name) 
        return out_video_name

    def infer_once(self, inp):
        self.inp = inp
        samples = self.get_cond_from_input(inp)
        batches = self.get_pose_from_ds(samples)
        image_dir = self.forward_system(batches)
        out_name = self.postprocess_output(image_dir)
        print(f"The synthesized video is saved at {out_name}")

    @classmethod
    def example_run(cls, inp=None):
        from utils.commons.hparams import set_hparams
        from utils.commons.hparams import hparams as hp
        set_hparams()
        inp_tmp = {
            'audio_source_name': 'data/raw/val_wavs/zozo.wav',
            'out_dir': 'infer_out',
            'out_video_name': 'infer_out/zozo.mp4'
            }
        if inp is not None:
            inp_tmp.update(inp)
        inp = inp_tmp
        if hparams.get("infer_cond_name", '') != '':
            inp['cond_name'] = hparams['infer_cond_name']
        if hparams.get("infer_audio_source_name", '') != '':
            inp['audio_source_name'] = hparams['infer_audio_source_name'] 
        if hparams.get("infer_out_video_name", '') != '':
            inp['out_video_name'] = hparams['infer_out_video_name']
        out_dir = os.path.dirname(inp['out_video_name'])
        video_name = os.path.basename(inp['out_video_name'])[:-4]
        tmp_imgs_dir = os.path.join(out_dir, "tmp_imgs", video_name)
        inp['tmp_imgs_dir'] = tmp_imgs_dir

        os.makedirs(out_dir, exist_ok=True)
        os.makedirs(tmp_imgs_dir, exist_ok=True)
        infer_ins = cls(hp)
        infer_ins.infer_once(inp)

    ##############
    # IO-related
    ##############
    @classmethod
    def save_mp4(self, img_dir, wav_name, out_name):
        os.system(f"ffmpeg -i {img_dir}/%5d.png -i {wav_name} -shortest -c:v libx264 -pix_fmt yuv420p -b:v 2000k -r 25 -strict -2 {out_name}")

    def save_wav16k(self, inp):
        source_name = inp['audio_source_name']
        supported_types = ('.wav', '.mp3', '.mp4', '.avi')
        assert source_name.endswith(supported_types), f"Now we only support {','.join(supported_types)} as audio source!"
        wav16k_name = source_name[:-4] + '_16k.wav'
        self.wav16k_name = wav16k_name
        extract_wav_cmd = f"ffmpeg -i {source_name} -f wav -ar 16000 {wav16k_name} -y"
        os.system(extract_wav_cmd)
        print(f"I have extracted wav file (16khz) from {source_name} to {wav16k_name}.")
    
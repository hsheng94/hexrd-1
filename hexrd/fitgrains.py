from __future__ import absolute_import

import copy
import logging
import multiprocessing as mp
from multiprocessing.queues import Empty
import os
import sys
import time

import yaml

import numpy as np
from scipy.sparse import coo_matrix
from scipy.linalg.matfuncs import logm

from hexrd.coreutil import initialize_experiment, migrate_detector_config
from hexrd.matrixutil import vecMVToSymm
from hexrd.utils.progressbar import (
    Bar, ETA, Percentage, ProgressBar, ReverseBar
    )

from hexrd.xrd import distortion as dFuncs
from hexrd.xrd.fitting import fitGrain, objFuncFitGrain
from hexrd.xrd.rotations import angleAxisOfRotMat, rotMatOfQuat
from hexrd.xrd.transforms import bVec_ref, eta_ref, mapAngle, vInv_ref
from hexrd.xrd.xrdutil import pullSpots


logger = logging.getLogger(__name__)


# grain parameter refinement flags
gFlag = np.array([1, 1, 1,
                  1, 1, 1,
                  1, 1, 1, 1, 1, 1], dtype=bool)
# grain parameter scalings
gScl  = np.array([1., 1., 1.,
                  1., 1., 1.,
                  1., 1., 1., 0.01, 0.01, 0.01])


def read_frames(reader, cfg):
    start = time.time()

    n_frames = reader.getNFrames()
    logger.info("reading %d frames of data", n_frames)
    widgets = [Bar('>'), ' ', ETA(), ' ', ReverseBar('<')]
    pbar = ProgressBar(widgets=widgets, maxval=n_frames).start()

    frame_list = []
    for i in range(n_frames):
        frame = reader.read()
        frame[frame <= cfg.fit_grains.threshold] = 0
        frame_list.append(coo_matrix(frame))
        pbar.update(i)
    frame_list = np.array(frame_list)
    omega_start = np.radians(cfg.image_series.omega.start)
    omega_step = np.radians(cfg.image_series.omega.step)
    reader = [frame_list, [omega_start, omega_step]]
    pbar.finish()
    return reader


def fit_grain():
    pass



def fit_grains(cfg, force=False):

    pd, reader, detector = initialize_experiment(cfg)

    # attempt to load the new detector parameter file
    det_p = cfg.detector.parameters
    if not os.path.exists(det_p):
        migrate_detector_config(
            np.loadtxt(cfg.detector.parameters_old),
            cfg.detector.pixels.rows,
            cfg.detector.pixels.columns,
            cfg.detector.pixels.size,
            detID='GE',
            chi=0.,
            tVec_s=np.zeros(3),
            filename=cfg.detector.parameters
            )

    with open(det_p, 'r') as f:
        # only one panel for now
        # TODO: configurize this
        instr_cfg = [instr_cfg for instr_cfg in yaml.load_all(f)][0]
    detector_params = np.hstack([
        instr_cfg['detector']['transform']['tilt_angles'],
        instr_cfg['detector']['transform']['t_vec_d'],
        instr_cfg['oscillation_stage']['chi'],
        instr_cfg['oscillation_stage']['t_vec_s'],
        ])
    # ***FIX***
    # at this point we know we have a GE and hardwire the distortion func;
    # need to pull name from yml file in general case
    distortion = (
        dFuncs.GE_41RT, instr_cfg['detector']['distortion']['parameters']
        )

    tth_max = cfg.fit_grains.tth_max
    if tth_max is True:
        pd.exclusions = np.zeros_like(pd.exclusions, dtype=bool)
        pd.exclusions = pd.getTTh() > detector.getTThMax()
    elif tth_max > 0:
        pd.exclusions = np.zeros_like(pd.exclusions, dtype=bool)
        pd.exclusions = pd.getTTh() >= np.radians(tth_max)

    # load quaternion file
    quats = np.atleast_2d(
        np.loadtxt(os.path.join(cfg.analysis_dir, 'quats.out'))
        )
    n_quats = len(quats)
    quats = quats.T

    # load the data
    reader = read_frames(reader, cfg)

    logger.info("fitting grains for %d orientations", n_quats)
    pbar = ProgressBar(
        widgets=[Bar('>'), ' ', ETA(), ' ', ReverseBar('<')],
        maxval=n_quats
        ).start()

    # create the job queue
    job_queue = mp.JoinableQueue()
    manager = mp.Manager()
    results = manager.list()

    # load the queue
    phi, n = angleAxisOfRotMat(rotMatOfQuat(quats))
    for i, quat in enumerate(quats.T):
        exp_map = phi[i]*n[:, i]
        grain_params = np.hstack(
            [exp_map.flatten(), 0., 0., 0., 1., 1., 1., 0., 0., 0.]
            )
        job_queue.put((i, grain_params))

    # don't query these in the loop, it will spam the logger:
    pkwargs = {
        'distortion': distortion,
        'omega_start': cfg.image_series.omega.start,
        'omega_step': cfg.image_series.omega.step,
        'omega_stop': cfg.image_series.omega.start \
            + len(reader[0]) * cfg.image_series.omega.step,
        'eta_range': np.radians(cfg.find_orientations.eta.range),
        'omega_period': np.radians(cfg.find_orientations.omega.period),
        'tth_tol': cfg.fit_grains.tolerance.tth,
        'eta_tol': cfg.fit_grains.tolerance.eta,
        'omega_tol': cfg.fit_grains.tolerance.omega,
        'panel_buffer': cfg.fit_grains.panel_buffer,
        'npdiv': cfg.fit_grains.npdiv,
        'threshold': cfg.fit_grains.threshold,
        'spots_stem': os.path.join(cfg.analysis_dir, 'spots_%05d.out'),
        'plane_data': pd,
        'detector_params': detector_params,
        }

    # finally start processing data
    ncpus = cfg.multiprocessing
    logging.info('running pullspots with %d processors')
    for i in range(ncpus):
        # lets make a deep copy of the pkwargs, just in case:
        w = FitGrainsWorker(job_queue, results, reader, copy.deepcopy(pkwargs))
        w.daemon = True
        w.start()
    while True:
        n_res = len(results)
        pbar.update(n_res)
        if n_res == n_quats:
            break
    job_queue.join()

    # record the results to file
    f = open(os.path.join(cfg.analysis_dir, 'grains.out'), 'w')
    # going to some length to make the header line up with the data
    # while also keeping the width of the lines to a minimum, settled
    # on %14.7g representation.
    header_items = (
        'grain ID', 'completeness', 'sum(resd**2)/nrefl',
        'xi[0]', 'xi[1]', 'xi[2]', 'tVec_c[0]', 'tVec_c[1]', 'tVec_c[2]',
        'vInv_s[0]', 'vInv_s[1]', 'vInv_s[2]', 'vInv_s[4]*sqrt(2)',
        'vInv_s[5]*sqrt(2)', 'vInv_s[6]*sqrt(2)', 'ln(V[0,0])',
        'ln(V[1,1])', 'ln(V[2,2])', 'ln(V[1,2])', 'ln(V[0,2])', 'ln(V[0,1])',
        )
    len_items = []
    for i in header_items[1:]:
        temp = len(i)
        len_items.append(temp if temp > 14 else 14) # for %14.7g
    fmtstr = '#%8s  ' + '  '.join(['%%%ds' % i for i in len_items]) + '\n'
    f.write(fmtstr % header_items)
    for (id, g_refined, compl, eMat, resd) in sorted(results):
        res_items = (
            id, compl, resd, g_refined[0], g_refined[1], g_refined[2],
            g_refined[3], g_refined[4], g_refined[5], g_refined[6],
            g_refined[7], g_refined[8], g_refined[9], g_refined[10],
            g_refined[11], eMat[0, 0], eMat[1, 1], eMat[2, 2], eMat[1, 2],
            eMat[0, 2], eMat[0, 1],
            )
        fmtstr = '%9d  ' + '  '.join(['%%%d.7g' % i for i in len_items]) + '\n'
        f.write(fmtstr % res_items)

    pbar.finish()



class FitGrainsWorker(mp.Process):


    def __init__(self, jobs, results, reader, pkwargs):
        super(FitGrainsWorker, self).__init__()
        self._jobs = jobs
        self._results = results
        self._reader = reader
        # a dict containing the rest of the parameters
        self._p = pkwargs

        # lets make a couple shortcuts:
        self._p['bMat'] = self._p['plane_data'].latVecOps['B']
        self._p['wlen'] = self._p['plane_data'].wavelength


    def pull_spots(self, grain_id, grain_params, iteration):
        return pullSpots(
            self._p['plane_data'],
            self._p['detector_params'],
            grain_params,
            self._reader,
            distortion=self._p['distortion'],
            eta_range=self._p['eta_range'],
            ome_period=self._p['omega_period'],
            eta_tol=self._p['eta_tol'][iteration],
            ome_tol=self._p['omega_tol'][iteration],
            tth_tol=self._p['tth_tol'][iteration],
            panel_buff=self._p['panel_buffer'],
            npdiv=self._p['npdiv'],
            threshold=self._p['threshold'],
            doClipping=False,
            filename=self._p['spots_stem'] % grain_id,
            )


    def fit_grains(self, grain_id, grain_params):
        ome_start = self._p['omega_start']
        ome_step = self._p['omega_step']
        ome_stop =  self._p['omega_stop']
        gtable = np.loadtxt(self._p['spots_stem'] % grain_id)
        valid_refl_ids = gtable[:, 0] >= 0
        pred_ome = gtable[:, 6]
        if np.sign(ome_step) < 0:
            idx_ome = np.logical_and(
                pred_ome < np.radians(ome_start + 2*ome_step),
                pred_ome > np.radians(ome_stop - 2*ome_step)
                )
        else:
            idx_ome = np.logical_and(
                pred_ome > np.radians(ome_start + 2*ome_step),
                pred_ome < np.radians(ome_stop - 2*ome_step)
                )

        idx = np.logical_and(valid_refl_ids, idx_ome)
        hkls = gtable[idx, 1:4].T # must be column vectors
        self._p['hkls'] = hkls
        xyo_det = gtable[idx, -3:] # these are the cartesian centroids + ome
        xyo_det[:, 2] = mapAngle(xyo_det[:, 2], self._p['omega_period'])
        self._p['xyo_det'] = xyo_det
        if sum(idx) <= 12:
            return grain_params, 0
        grain_params = fitGrain(
            xyo_det, hkls, self._p['bMat'], self._p['wlen'],
            self._p['detector_params'],
            grain_params[:3], grain_params[3:6], grain_params[6:],
            beamVec=bVec_ref, etaVec=eta_ref,
            distortion=self._p['distortion'],
            gFlag=gFlag, gScl=gScl,
            omePeriod=self._p['omega_period']
            )
        completeness = sum(idx)/float(len(idx))
        return grain_params, completeness


    def loop(self):
        id, grain_params = self._jobs.get(False)

        for iteration in range(2):
            self.pull_spots(id, grain_params, iteration)
            grain_params, compl = self.fit_grains(id, grain_params)
            if compl == 0:
                break

        eMat = logm(np.linalg.inv(vecMVToSymm(grain_params[6:])))

        dFunc, dParams = self._p['distortion']
        resd = objFuncFitGrain(
            grain_params[gFlag], grain_params, gFlag,
            self._p['detector_params'],
            self._p['xyo_det'], self._p['hkls'],
            self._p['bMat'], self._p['wlen'],
            bVec_ref, eta_ref,
            dFunc, dParams,
            self._p['omega_period'],
            simOnly=False
            )

        self._results.append((id, grain_params, compl, eMat, sum(resd**2)))
        self._jobs.task_done()


    def run(self):
        while True:
            try:
                self.loop()
            except Empty:
                break
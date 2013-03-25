import sys
import os
import socket
import time
import h5py
import numpy as np
import larch

from larch.utils.debugtime import debugtime

sys.path.insert(0, larch.plugin_path('io'))
from fileutils import nativepath

sys.path.insert(0, larch.plugin_path('xrf'))
from mca import MCA

sys.path.insert(0, larch.plugin_path('xrfmap'))
from configfile import FastMapConfig
from xmap_netcdf import read_xmap_netcdf
from asciifiles import (readASCII, readMasterFile, readROIFile,
                        readEnvironFile, parseEnviron)

NINIT = 16
COMP = 4 # compression level

class GSEXRM_FileStatus:
    no_xrfmap    = 'hdf5 does not have /xrfmap'
    created      = 'hdf5 has empty schema'  # xrfmap exists, no data
    hasdata      = 'hdf5 has map data'      # array sizes known
    err_notfound = 'file not found'
    err_nothdf5  = 'file is not hdf5'

def getFileStatus(filename):
    # see if file exists:
    if (not os.path.exists(filename) or
        not os.path.isfile(filename) ):
        return GSEXRM_FileStatus.err_notfound

    # see if file is an H5 file
    try:
        fh = h5py.File(filename)
    except IOError:
        return GSEXRM_FileStatus.err_nothdf5
    if 'xrfmap' not in fh:
        return GSEXRM_FileStatus.no_xrfmap

    if 'det1' in fh['/xrfmap']:
        return GSEXRM_FileStatus.hasdata
    fh.close()
    return GSEXRM_FileStatus.created

def isGSEXRM_MapFolder(fname):
    "return whether folder a valid Scan Folder (raw data)"
    if (fname is None or not os.path.exists(fname) or
        not os.path.isdir(fname)):
        return False
    flist = os.listdir(fname)
    for f in ('Master.dat', 'Environ.dat', 'Scan.ini', 'xmap.0001'):
        if f not in flist:
            return False
    return True

H5ATTRS = {'Version': '1.3.0',
           'Title': 'Epics Scan Data',
           'Beamline': 'GSECARS, 13-IDE / APS',
           'Start_Time':'',
           'Stop_Time':'',
           'Dimension': 2,
           'Process_Machine':'',
           'Process_ID': 0}

def create_xrfmap(h5root, dimension=2, folder='', start_time=None):
    """creates a skeleton '/xrfmap' group in an open HDF5 file

    This is left as a function, not method of GSEXRM_MapFile below
    because it may be called by the mapping collection program
    (ie, from collector.py) when a map is started

    This leaves a structure to be filled in by
    GSEXRM_MapFile.init_xrfmap(),
    """
    attrs = {}
    attrs.update(H5ATTRS)
    if start_time is None:
        start_time = time.ctime()
    attrs.update({'Dimension':dimension, 'Start_Time':start_time,
                  'Map_Folder': folder, 'Last_Row': -1})

    xrfmap = h5root.create_group('xrfmap')
    for key, val in attrs.items():
        xrfmap.attrs[key] = str(val)

    g = xrfmap.create_group('roimap')
    g.attrs['type'] = 'roi maps'
    g.attrs['desc'] = 'ROI data, including summed and deadtime corrected maps'

    g = xrfmap.create_group('config')
    g.attrs['type'] = 'scan config'
    g.attrs['desc'] = '''scan configuration, including scan definitions,
    ROI definitions, MCA calibration, Environment Data, etc'''

    xrfmap.create_group('areas')
    xrfmap.create_group('positions')

    conf = xrfmap['config']
    for name in ('scan', 'general', 'environ', 'positioners',
                 'motor_controller', 'rois', 'mca_settings', 'mca_calib'):
        conf.create_group(name)
    h5root.flush()

class GSEXRM_Exception(Exception):
    """GSEXRM Exception: General Errors"""
    def __init__(self, msg):
        Exception.__init__(self)
        self.msg = msg
    def __str__(self):
        return self.msg

class GSEXRM_NotOwner(Exception):
    """GSEXRM Not Owner Host/Process ID"""
    def __init__(self, msg):
        Exception.__init__(self)
        self.msg = 'Not Owner of HDF5 file %s' % msg
    def __str__(self):
        return self.msg

class GSEXRM_MapRow:
    """
    read one row worth of data:
    """
    def __init__(self, yvalue, xmapfile, xpsfile, sisfile, folder,
                 reverse=False, ixaddr=0, dimension=2, npts=None,
                 irow=None, dtime=None):
        self.npts = npts
        self.irow = irow
        self.yvalue = yvalue
        self.xmapfile = xmapfile
        self.xpsfile = xpsfile
        self.sisfile = sisfile

        shead, sdata = readASCII(os.path.join(folder, sisfile))
        ghead, gdata = readASCII(os.path.join(folder, xpsfile))
        self.sishead = shead
        if dtime is not None:  dtime.add('maprow: read ascii files')
        t0 = time.time()
        atime = -1
        xmapdat = None
        xmfile = os.path.join(folder, xmapfile)
        while atime < 0 and time.time()-t0 < 10:
            try:
                atime = os.stat(xmfile).st_ctime
                xmapdat = read_xmap_netcdf(xmfile, verbose=False)
            except (IOError, IndexError):
                time.sleep(0.010)

        if atime < 0 or xmapdat is None:
            print 'Failed to read xmap data from %s' % self.xmapfile
            return
        if dtime is not None:  dtime.add('maprow: read xmap files')
        #
        self.counts    = xmapdat.counts # [:]
        self.inpcounts = xmapdat.inputCounts[:]
        self.outcounts = xmapdat.outputCounts[:]
        den = self.outcounts[:]*1.0000
        den[np.where(den<1)] = 1.00
        self.dtfactor  = (xmapdat.inputCounts)/den
        # times are extracted from the netcdf file as floats of microseconds
        # here we truncate to nearest microsecond (clock tick is 0.32 microseconds)
        self.livetime  = (xmapdat.liveTime[:]).astype('int')
        self.realtime  = (xmapdat.realTime[:]).astype('int')

        gnpts, ngather  = gdata.shape
        snpts, nscalers = sdata.shape
        xnpts, nmca, nchan = self.counts.shape
        npts = min(gnpts, xnpts)
        if self.npts is None:
            self.npts = npts
        if snpts < self.npts:  # extend struck data if needed
            sdata = list(sdata)
            for i in range(self.npts+1-snpts):
                sdata.append(sdata[snpts-1])
            sdata = np.array(sdata)
            snpts = self.npts
        self.sisdata = sdata

        if xnpts != npts:
            self.counts  = self.counts[:npts]
            self.realtime = self.realtime[:npts]
            self.livetime = self.livetime[:npts]
            self.dtfactor = self.dtfactor[:npts]
            self.inpcounts= self.inpcounts[:npts]
            self.outcounts= self.outcounts[:npts]

        points = range(1, npts+1)
        if reverse:
            points.reverse()
            self.sisdata  = self.sisdata[::-1]
            self.counts  = self.counts[::-1]
            self.realtime = self.realtime[::-1]
            self.livetime = self.livetime[::-1]
            self.dtfactor = self.dtfactor[::-1]
            self.inpcounts= self.inpcounts[::-1]
            self.outcounts= self.outcounts[::-1]

        xvals = [(gdata[i, ixaddr] + gdata[i-1, ixaddr])/2.0 for i in points]

        self.posvals = [np.array(xvals)]
        if dimension == 2:
            self.posvals.append(np.array([float(yvalue) for i in points]))
        self.posvals.append(self.realtime.sum(axis=1).astype('float32') / nmca)
        self.posvals.append(self.livetime.sum(axis=1).astype('float32') / nmca)


        total = None
        for imca in range(nmca):
            dtcorr = self.dtfactor[:, imca].astype('float32')
            cor   = dtcorr.reshape((dtcorr.shape[0], 1))
            if total is None:
                total = self.counts[:, imca, :] * cor
            else:
                total = total + self.counts[:, imca, :] * cor
        self.total = total.astype('int16')
        self.dtfactor = self.dtfactor.astype('float32')
        self.dtfactor = self.dtfactor.transpose()
        self.inpcounts= self.inpcounts.transpose()
        self.outcounts= self.outcounts.transpose()
        self.livetime = self.livetime.transpose()
        self.realtime = self.realtime.transpose()
        self.counts   = self.counts.swapaxes(0, 1)

class GSEXRM_MapFile(object):
    """
    Access to GSECARS X-ray Microprobe Map File:

    The GSEXRM Map file is an HDF5 file built from a folder containing
    'raw' data from a set of sources
         xmap:   XRF spectra saved to NetCDF by the Epics MCA detector
         struck: a multichannel scaler, saved as ASCII column data
         xps:    stage positions, saved as ASCII file from the Newport XPS

    The object here is intended to expose an HDF5 file that:
         a) watches the corresponding folder and auto-updates when new
            data is available, as for on-line collection
         b) stores locking information (Machine Name/Process ID) in the top-level

    For extracting data from a GSEXRM Map File, use:

    >>> from epicscollect.io import GSEXRM_MapFile
    >>> map = GSEXRM_MapFile('MyMap.001')
    >>> fe  = map.get_roimap('Fe')
    >>> as  = map.get_roimap('As Ka', det=1, dtcorrect=True)
    >>> rgb = map.get_rgbmap('Fe', 'Ca', 'Zn', det=None, dtcorrect=True, scale_each=False)
    >>> en  = map.get_energy(det=1)
    >>> xrf = map.get_spectra(xmin=10, xmax=20, ymin=40, ymax=50, dtcorrect=True)

    All these take the following options:

       det:         which detector element to use (1, 2, 3, 4, None), [None]
                    None means to use the sum of all detectors
       dtcorrect:   whether to return dead-time corrected spectra     [True]

    """

    ScanFile   = 'Scan.ini'
    EnvFile    = 'Environ.dat'
    ROIFile    = 'ROI.dat'
    MasterFile = 'Master.dat'

    def __init__(self, filename=None, folder=None):
        self.filename = filename
        self.folder   = folder
        self.status   = GSEXRM_FileStatus.err_notfound
        self.dimension = None
        self.start_time = None
        self.xrfmap   = None
        self.h5root   = None
        self.last_row = -1
        self.rowdata = []
        self.npts = None
        self.roi_slices = None
        self.dt = debugtime()

        # initialize from filename or folder
        if self.filename is not None:
            self.status   = getFileStatus(self.filename)

        elif isGSEXRM_MapFolder(self.folder):
            self.read_master()
            if self.filename is None:
                raise GSEXRM_Exception(
                    "'%s' is not a valid GSEXRM Map folder" % self.folder)
            self.status   = getFileStatus(self.filename)

        # for existing file, read initial settings
        if self.status in (GSEXRM_FileStatus.hasdata,
                           GSEXRM_FileStatus.created):
            self.open(self.filename, check_status=False)
            return

        # file exists but is not hdf5
        if self.status ==  GSEXRM_FileStatus.err_nothdf5:
            raise GSEXRM_Exception(
                "'%s' is not an HDF5 file" % self.filename)

        # create empty HDF5 if needed
        if (self.status == GSEXRM_FileStatus.err_notfound and
            self.folder is not None and isGSEXRM_MapFolder(self.folder)):
            self.read_master()
            self.h5root = h5py.File(self.filename)
            if self.dimension is None and isGSEXRM_MapFolder(self.folder):
                self.read_master()
            create_xrfmap(self.h5root, dimension=self.dimension,
                          folder=self.folder, start_time=self.start_time)
            self.status = GSEXRM_FileStatus.created
            self.open(self.filename, check_status=False)
        else:
            raise GSEXRM_Exception(
                "'GSEXMAP Error: could not locate map file or folder")

    def open(self, filename, check_status=True):
        """open GSEXRM HDF5 File :
        with check_status=False, this **must** be called
        for an existing, valid GSEXRM HDF5 File!!
        """
        if check_status:
            self.status   = getFileStatus(filename)
            if self.status not in (GSEXRM_FileStatus.hasdata,
                                   GSEXRM_FileStatus.created):
                raise GSEXRM_Exception(
                    "'%s' is not a valid GSEXRM HDF5 file" % self.filename)
        self.filename = filename
        if self.h5root is None:
            self.h5root = h5py.File(self.filename)
        self.xrfmap = self.h5root['/xrfmap']
        if self.folder is None:
            self.folder = self.xrfmap.attrs['Map_Folder']
        self.last_row = self.xrfmap.attrs['Last_Row']

        try:
            self.dimension = self.xrfmap['config/scan/dimension'].value
        except:
            pass

        if self.dimension is None and isGSEXRM_MapFolder(self.folder):
            self.read_master()

    def close(self):
        if self.check_hostid():
            self.xrfmap.attrs['Process_Machine'] = ''
            self.xrfmap.attrs['Process_ID'] = 0
            self.xrfmap.attrs['Last_Row'] = self.last_row
        self.h5root.close()
        self.h5root = None

    def add_data(self, group, name, data, attrs=None, **kws):
        """ creata an hdf5 dataset"""
        if not self.check_hostid():
            raise GSEXRM_NotOwner(self.filename)

        kwargs = {'compression': 4}
        kwargs.update(kws)
        d = group.create_dataset(name, data=data, **kwargs)
        if isinstance(attrs, dict):
            for key, val in attrs.items():
                d.attrs[key] = val
        return d

    def add_map_config(self, config):
        """add configuration from Map Folder to HDF5 file
        ROI, DXP Settings, and Config data
        """
        if not self.check_hostid():
            raise GSEXRM_NotOwner(self.filename)

        group = self.xrfmap['config']
        scantext = open(os.path.join(self.folder, self.ScanFile), 'r').read()
        for name, sect in (('scan', 'scan'),
                           ('general', 'general'),
                           ('positioners', 'slow_positioners'),
                           ('motor_controller', 'xps')):
            for key, val in config[sect].items():
                group[name].create_dataset(key, data=val)

        group['scan'].create_dataset('text', data=scantext)

        roidat, calib, dxp = readROIFile(os.path.join(self.folder, self.ROIFile))
        roi_desc, roi_addr, roi_lim = [], [], []
        roi_slices = []
        for iroi, label, lims in roidat:
            roi_desc.append(label)
            roi_addr.append("%smca%%i.R%i" % (config['general']['xmap'], iroi))
            roi_lim.append([lims[i] for i in range(4)])
            roi_slices.append([slice(lims[i][0], lims[i][1]) for i in range(4)])
        roi_lim = np.array(roi_lim)

        self.add_data(group['rois'], 'name',     roi_desc)
        self.add_data(group['rois'], 'address',  roi_addr)
        self.add_data(group['rois'], 'limits',   roi_lim)

        for key, val in calib.items():
            self.add_data(group['mca_calib'], key, val)

        for key, val in dxp.items():
            self.add_data(group['mca_settings'], key, val)

        self.roi_desc = roi_desc
        self.roi_addr = roi_addr
        self.roi_slices = roi_slices
        self.calib = calib
        # add env data
        envdat = readEnvironFile(os.path.join(self.folder, self.EnvFile))
        env_desc, env_addr, env_val = parseEnviron(envdat)

        self.add_data(group['environ'], 'name',     env_desc)
        self.add_data(group['environ'], 'address',  env_addr)
        self.add_data(group['environ'], 'value',     env_val)
        self.h5root.flush()

    def initialize_xrfmap(self):
        """ initialize '/xrfmap' group in HDF5 file, generally
        possible once at least 1 row of raw data is available
        in the scan folder.
        """
        if self.status == GSEXRM_FileStatus.hasdata:
            return
        if self.status != GSEXRM_FileStatus.created:
            print 'Warning, cannot initialize xrfmap yet.'
            return

        if not self.check_hostid():
            raise GSEXRM_NotOwner(self.filename)

        if self.dimension is None and isGSEXRM_MapFolder(self.folder):
            self.read_master()
        self.npts = None
        if len(self.rowdata) < 1:
            return
        self.last_row = -1
        self.add_map_config(self.mapconf)
        row = self.read_rowdata(0)
        self.build_schema(row)
        self.add_rowdata(row)
        self.status = GSEXRM_FileStatus.hasdata

    def process(self, maxrow=None, force=False, callback=None, verbose=True):
        "look for more data from raw folder, process if needed"

        if not self.check_hostid():
            raise GSEXRM_NotOwner(self.filename)

        if self.status == GSEXRM_FileStatus.created:
            self.initialize_xrfmap()
        if force or (self.dimension is None and isGSEXRM_MapFolder(self.folder)):
            self.read_master()
        nrows = len(self.rowdata)
        if maxrow is not None:
            nrows = min(nrows, maxrow)

        if force or self.folder_has_newdata():
            irow = self.last_row + 1
            while irow < nrows:
                # self.dt.add('=>PROCESS %i' % irow)
                if hasattr(callback, '__call__'):
                    callback(row=irow, maxrow=nrows,
                             filename=self.filename, status='reading')
                row = self.read_rowdata(irow)
                # self.dt.add('  == read row data')
                if row is not None:
                    self.add_rowdata(row, verbose=verbose)
                # self.dt.add('  == added row data')
                if hasattr(callback, '__call__'):
                    callback(row=irow, maxrow=nrows,
                             filename=self.filename, status='complete')
                irow  = irow + 1
            # self.dt.show()

        self.resize_arrays(self.last_row+1)
        self.h5root.flush()

    def read_rowdata(self, irow):
        """read a row's worth of raw data from the Map Folder
        returns arrays of data
        """
        if self.dimension is None or irow > len(self.rowdata):
            self.read_master()

        if self.folder is None or irow >= len(self.rowdata):
            return

        yval, xmapf, sisf, xpsf, etime = self.rowdata[irow]
        reverse = (irow % 2 != 0)
        # print 'Read Row ', irow
        return GSEXRM_MapRow(yval, xmapf, xpsf, sisf, irow=irow,
                             ixaddr=self.ixaddr,
                             dimension=self.dimension, npts=self.npts,
                             folder=self.folder, reverse=reverse)

    def add_rowdata(self, row, verbose=True):
        """adds a row worth of real data"""
        if not self.check_hostid():
            raise GSEXRM_NotOwner(self.filename)
        thisrow = self.last_row + 1
        nmca, xnpts, nchan = row.counts.shape
        mcas = []
        map_items = sorted(self.xrfmap.keys())
        for gname in map_items:
            g = self.xrfmap[gname]
            if g.attrs.get('type', None) == 'mca detector':
                mcas.append(g)
                nrows, npts, nchan =  g['counts'].shape

        # print 'XRFMAP Add Row ', thisrow, nrows, npts, nchan
        if thisrow >= nrows:
            self.resize_arrays(32*(1+nrows/32))

        total = None
        # self.dt.add('add_rowdata b4 adding mcas')
        for imca, grp in enumerate(mcas):
            grp['dtfactor'][thisrow, :]  = row.dtfactor[imca, :]
            grp['realtime'][thisrow, :]  = row.realtime[imca, :]
            grp['livetime'][thisrow, :]  = row.livetime[imca, :]
            grp['inpcounts'][thisrow, :] = row.inpcounts[imca, :]
            grp['outcounts'][thisrow, :] = row.outcounts[imca, :]
            grp['counts'][thisrow, :, :] = row.counts[imca, :, :]

        # self.dt.add('add_rowdata for mcas')
        # here, we add the total dead-time-corrected data to detsum.
        self.xrfmap['detsum']['counts'][thisrow, :] = row.total[:]
        # self.dt.add('add_rowdata for detsum')

        pos    = self.xrfmap['positions/pos']
        pos[thisrow, :, :] = np.array(row.posvals).transpose()

        # now add roi map data
        roimap = self.xrfmap['roimap']
        det_raw = roimap['det_raw']
        det_cor = roimap['det_cor']
        sum_raw = roimap['sum_raw']
        sum_cor = roimap['sum_cor']

        detraw = list(row.sisdata[:npts].transpose())

        if verbose:
            pform ="Add row=%4i, yval=%s, npts=%i, xmapfile=%s"
            print pform % (thisrow+1, row.yvalue, npts, row.xmapfile)
            # print row.yvalue, row.inpcounts.sum()
            # print '  sis : ', row.sisdata[2:5,:]

        detcor = detraw[:]
        sumraw = detraw[:]
        sumcor = detraw[:]

        # self.dt.add('add_rowdata b4 roi')
        if self.roi_slices is None:
            lims = self.xrfmap['config/rois/limits'].value
            nrois, nmca, nx = lims.shape
            self.roi_slices = []
            for iroi in range(nrois):
                x = [slice(lims[iroi, i, 0],
                           lims[iroi, i, 1]) for i in range(nmca)]
                self.roi_slices.append(x)

        for slices in self.roi_slices:
            iraw = [row.counts[i, :, slices[i]].sum(axis=1)
                    for i in range(nmca)]
            icor = [row.counts[i, :, slices[i]].sum(axis=1)*row.dtfactor[i, :]
                    for i in range(nmca)]
            detraw.extend(iraw)
            detcor.extend(icor)
            sumraw.append(np.array(iraw).sum(axis=0))
            sumcor.append(np.array(icor).sum(axis=0))

        # self.dt.add('add_rowdata after roi')
        det_raw[thisrow, :, :] = np.array(detraw).transpose()
        det_cor[thisrow, :, :] = np.array(detcor).transpose()
        sum_raw[thisrow, :, :] = np.array(sumraw).transpose()
        sum_cor[thisrow, :, :] = np.array(sumcor).transpose()

        # self.dt.add('add_rowdata end')
        self.last_row = thisrow
        self.xrfmap.attrs['Last_Row'] = thisrow
        self.h5root.flush()

    def build_schema(self, row):
        """build schema for detector and scan data"""
        if not self.check_hostid():
            raise GSEXRM_NotOwner(self.filename)

        if self.npts is None:
            self.npts = row.npts
        npts = self.npts
        nmca, xnpts, nchan = row.counts.shape
        en_index = np.arange(nchan)

        xrfmap = self.xrfmap
        conf   = self.xrfmap['config']

        offset = conf['mca_calib/offset'].value
        slope  = conf['mca_calib/slope'].value
        quad   = conf['mca_calib/quad'].value

        roi_names = list(conf['rois/name'])
        roi_addrs = list(conf['rois/address'])
        roi_limits = conf['rois/limits'].value
        for imca in range(nmca):
            dname = 'det%i' % (imca+1)
            dgrp = xrfmap.create_group(dname)
            dgrp.attrs['type'] = 'mca detector'
            dgrp.attrs['desc'] = 'mca%i' % (imca+1)
            en  = 1.0*offset[imca] + slope[imca]*1.0*en_index
            self.add_data(dgrp, 'energy', en, attrs={'cal_offset':offset[imca],
                                                     'cal_slope': slope[imca]})

            self.add_data(dgrp, 'roi_names', roi_names)
            self.add_data(dgrp, 'roi_addrs', [s % (imca+1) for s in roi_addrs])
            self.add_data(dgrp, 'roi_limits', roi_limits[:,imca,:])

            dgrp.create_dataset('counts', (NINIT, npts, nchan), np.int16,
                                compression=COMP, maxshape=(None, npts, nchan))
            for name, dtype in (('realtime', np.int),  ('livetime', np.int),
                                ('dtfactor', np.float32),
                                ('inpcounts', np.float32),
                                ('outcounts', np.float32)):
                dgrp.create_dataset(name, (NINIT, npts), dtype,
                                    compression=COMP, maxshape=(None, npts))

        # add 'virtual detector' for corrected sum:
        dgrp = xrfmap.create_group('detsum')
        dgrp.attrs['type'] = 'virtual mca'
        dgrp.attrs['desc'] = 'deadtime corrected sum of detectors'
        en = 1.0*offset[0] + slope[0]*1.0*en_index
        self.add_data(dgrp, 'energy', en, attrs={'cal_offset':offset[0],
                                                 'cal_slope': slope[0]})
        self.add_data(dgrp, 'roi_names', roi_names)
        self.add_data(dgrp, 'roi_addrs', [s % 1 for s in roi_addrs])
        self.add_data(dgrp, 'roi_limits', roi_limits[: ,0, :])
        dgrp.create_dataset('counts', (NINIT, npts, nchan), np.int16,
                            compression=COMP, maxshape=(None, npts, nchan))

        # roi map data
        scan = xrfmap['roimap']
        det_addr = [i.strip() for i in row.sishead[-2][1:].split('|')]
        det_desc = [i.strip() for i in row.sishead[-1][1:].split('|')]
        for addr in roi_addrs:
            det_addr.extend([addr % (i+1) for i in range(nmca)])

        for desc in roi_names:
            det_desc.extend(["%s (mca%i)" % (desc, i+1)
                             for i in range(nmca)])

        sums_map = {}
        sums_desc = []
        nsum = 0
        for idet, addr in enumerate(det_desc):
            if '(mca' in addr:
                addr = addr.split('(mca')[0].strip()

            if addr not in sums_map:
                sums_map[addr] = []
                sums_desc.append(addr)
            sums_map[addr].append(idet)
        nsum = max([len(s) for s in sums_map.values()])
        sums_list = []
        for sname in sums_desc:
            slist = sums_map[sname]
            if len(slist) < nsum:
                slist.extend([-1]*(nsum-len(slist)))
            sums_list.append(slist)

        nsum = len(sums_list)
        sums_list = np.array(sums_list)
        nsca = len(det_desc)
        self.add_data(scan, 'det_name',    det_desc)
        self.add_data(scan, 'det_address', det_addr)
        self.add_data(scan, 'sum_name',    sums_desc)
        self.add_data(scan, 'sum_list',    sums_list)


        for name, nx, dtype in (('det_raw', nsca, np.int32),
                                ('det_cor', nsca, np.float32),
                                ('sum_raw', nsum, np.int32),
                                ('sum_cor', nsum, np.float32)):
            scan.create_dataset(name, (NINIT, npts, nx), dtype,
                                compression=COMP, maxshape=(None, npts, nx))

        # positions
        pos = xrfmap['positions']
        for pname in ('mca realtime', 'mca livetime'):
            self.pos_desc.append(pname)
            self.pos_addr.append(pname)
        npos = len(self.pos_desc)
        self.add_data(pos, 'name',     self.pos_desc)
        self.add_data(pos, 'address',  self.pos_addr)
        pos.create_dataset('pos', (NINIT, npts, npos), dtype,
                           compression=COMP, maxshape=(None, npts, npos))
        self.h5root.flush()

    def resize_arrays(self, nrow):
        "resize all arrays for new nrow size"
        if not self.check_hostid():
            raise GSEXRM_NotOwner(self.filename)
        realmca_groups = []
        virtmca_groups = []
        for g in self.xrfmap.values():
            # include both real and virtual mca detectors!
            if g.attrs.get('type', '').startswith('mca det'):
                realmca_groups.append(g)
            elif g.attrs.get('type', '').startswith('virtual mca'):
                virtmca_groups.append(g)
        oldnrow, npts, nchan = realmca_groups[0]['counts'].shape
        for g in realmca_groups:
            g['counts'].resize((nrow, npts, nchan))
            for aname in ('livetime', 'realtime',
                          'inpcounts', 'outcounts', 'dtfactor'):
                g[aname].resize((nrow, npts))

        for g in virtmca_groups:
            g['counts'].resize((nrow, npts, nchan))

        g = self.xrfmap['positions/pos']
        old, npts, nx = g.shape
        g.resize((nrow, npts, nx))

        for bname in ('det_raw', 'det_cor', 'sum_raw', 'sum_cor'):
            g = self.xrfmap['roimap'][bname]
            old, npts, nx = g.shape
            g.resize((nrow, npts, nx))
        self.h5root.flush()

    def add_area(self, mask, name=None, desc=None):
        """add a selected area, with optional name
        the area is encoded as a boolean array the same size as the map

        """
        if not self.check_hostid():
            raise GSEXRM_NotOwner(self.filename)

        group = self.xrfmap['areas']
        name = 'area_1'
        if len(group) > 0:
            count = len(group)
            while name in group and count < 9999:
                name = 'area_%i' % (count)
                count += 1
        ds = group.create_dataset(name, data=mask)
        if desc is None:
            desc = name
        ds.attrs['description'] = desc
        self.h5root.flush()
        return name

    def get_area(self, name=None, desc=None):
        """
        get area by name or description
        """
        group = self.xrfmap['areas']
        if name is not None and name in group:
            return group[name]
        if desc is not None:
            for name in group:
                if desc == group[name].attrs['description']:
                    return group[name]
        return None

    def claim_hostid(self):
        "claim ownershipf of file"
        if self.xrfmap is None:
            return
        self.xrfmap.attrs['Process_Machine'] = socket.gethostname()
        self.xrfmap.attrs['Process_ID'] = os.getpid()
        self.h5root.flush()

    def check_hostid(self):
        """checks host and id of file:
        returns True if this process the owner of the file
        """
        if self.xrfmap is None:
            return
        attrs = self.xrfmap.attrs
        self.folder = attrs['Map_Folder']

        file_mach = attrs['Process_Machine']
        file_pid  = attrs['Process_ID']
        if len(file_mach) < 1 or file_pid < 1:
            self.claim_hostid()
            return True
        return (file_mach == socket.gethostname() and
                file_pid == os.getpid())

    def folder_has_newdata(self):
        if self.folder is not None and isGSEXRM_MapFolder(self.folder):
            self.read_master()
            return (self.last_row < len(self.rowdata)-1)
        return False

    def read_master(self):
        "reads master file for toplevel scan info"
        if self.folder is None or not isGSEXRM_MapFolder(self.folder):
            return
        self.masterfile = os.path.join(nativepath(self.folder),
                                       self.MasterFile)
        try:
            header, rows = readMasterFile(self.masterfile)
        except IOError:
            raise GSEXRM_Exception(
                "cannot read Master file from '%s'" % self.masterfile)

        self.master_header = header
        self.rowdata = rows
        stime = self.master_header[0][6:]
        self.start_time = stime.replace('started at','').strip()

        self.folder_modtime = os.stat(self.masterfile).st_mtime
        self.stop_time = time.ctime(self.folder_modtime)

        cfile = FastMapConfig()
        cfile.Read(os.path.join(self.folder, self.ScanFile))
        self.mapconf = cfile.config

        if self.filename is None:
            self.filename = self.mapconf['scan']['filename']
        if not self.filename.endswith('.h5'):
            self.filename = "%s.h5" % self.filename

        mapconf = self.mapconf
        slow_pos = mapconf['slow_positioners']
        fast_pos = mapconf['fast_positioners']

        scanconf = mapconf['scan']
        self.dimension = scanconf['dimension']
        start = mapconf['scan']['start1']
        stop  = mapconf['scan']['stop1']
        step  = mapconf['scan']['step1']
        span = abs(stop-start)
        self.npts = int(abs(step*1.01 + span)/step)

        pos1 = scanconf['pos1']
        self.pos_addr = [pos1]
        self.pos_desc = [slow_pos[pos1]]
        self.ixaddr = -1
        for i, posname in enumerate(fast_pos):
            if posname == pos1:
                self.ixaddr = i
        if self.dimension > 1:
            yaddr = scanconf['pos2']
            self.pos_addr.append(yaddr)
            self.pos_desc.append(slow_pos[yaddr])

    def get_energy(self, det=None):
        """return energy array for a detector"""
        dgroup= 'detsum'
        if det in (1, 2, 3, 4):
            dgroup = 'det%i' % det

        return self.xrfmap["%s/energy" % dgroup].value

    def get_mca_rect(self, det=None, dtcorrect=True,
                         xmin=None, xmax=None, ymin=None, ymax=None):
        """return XRF spectra as MCA() instance,
        summed over a given rectangle.
        xmin/xmax/ymin/ymax given in pixel units of the map
        """
        dgroup= 'detsum'
        if det in (1, 2, 3, 4):
            dgroup = 'det%i' % det
        map = self.xrfmap[dgroup]
        xslice = slice(xmin, xmax)
        yslice = slice(ymin, ymax)

        counts= map['counts'][xslice, yslice, :]
        if dtcorrect and det in (1, 2, 3, 4):
            dtfact = map['dtfactor'][xslice, yslice]
            dtfact = dtfact.respace(dtfact.shape[0], dtfact.shape[1], 1)
            counts = counts.value * dtfact.value

        counts = counts.sum(axis=0).sum(axis=0)

        ### create an MCA
        energy  = map['energy'].value
        cal     = map['energy'].attrs

        thismca = MCA(counts=counts, offset=cal['cal_offset'],
                      slope=cal['cal_slope'])
        thismca.energy = energy
        roinames = map['roi_names'].value[:]
        roilims  = map['roi_limits'].value[:]
        for roi, lims in zip(roinames, roilims):
            thismca.add_roi(roi, left=lims[0], right=lims[1])
        return thismca

    def get_mca_area(self, areaname, det=None, dtcorrect=True):
        """
        return XRF spectra as MCA() instance for
        spectra summed over a pre-defined area
        """
        area = self.get_area(areaname).value
        if area is None:
            raise GSEXRM_Exception("Could not find area '%s'" % areaname)
        dgroup= 'detsum'
        if det in (1, 2, 3, 4):
            dgroup = 'det%i' % det
        map = self.xrfmap[dgroup]

        counts = map['counts'].value[area]

        if dtcorrect and det in (1, 2, 3, 4):
            dtfact = map['dtfactor'].value[area]
            dtfact = dtfact.respace(dtfact.shape[0], dtfact.shape[1], 1)
            counts = counts * dtfact
        counts = counts.sum(axis=0)

        ### create an MCA
        energy  = map['energy'].value
        cal     = map['energy'].attrs

        thismca = MCA(counts=counts, offset=cal['cal_offset'],
                      slope=cal['cal_slope'])
        thismca.energy = energy
        roinames = map['roi_names'].value[:]
        roilims  = map['roi_limits'].value[:]
        for roi, lims in zip(roinames, roilims):
            thismca.add_roi(roi, left=lims[0], right=lims[1])
        return thismca


    def get_pos(self, name, mean=True):
        """return  position by name (matching 'roimap/pos_name' if
        name is a string, or using name as an index if it is an integer

        with mean=True, and a positioner in the first two position,
        returns a 1-d array of mean x-values

        with mean=False, and a positioner in the first two position,
        returns a 2-d array of x values for each pixel
        """
        index = -1
        if isinstance(name, int):
            index = name
        else:
            for ix, nam in enumerate(self.xrfmap['positions/name']):
                if nam.lower() == nam.lower():
                    index = ix
                    break

        if index == -1:
            raise GSEXRM_Exception("Could not find position '%s'" % repr(name))
        pos = self.xrfmap['positions/pos'][:, :, index]
        if index in (0, 1) and mean:
            pos = pos.sum(axis=index)/pos.shape[index]
        return pos

    def get_roimap(self, name, det=None, dtcorrect=True):
        """extract roi map for a pre-defined roi by name
        """
        imap = -1
        if det in (1, 2, 3, 4):
            mcaname = '(mca%i)' % det
            names = list(self.xrfmap['roimap/det_name'])
            dat = 'roimap/det_raw'
            if dtcorrect:
                dat = 'roimap/det_cor'
        else:
            mcaname = ''
            names = list(self.xrfmap['roimap/sum_name'])
            dat = 'roimap/sum_raw'
            if dtcorrect:
                dat = 'roimap/sum_cor'

        for i, roiname in enumerate(names):
            if roiname.startswith(name) and roiname.endswith(mcaname):
                imap = i
                break
        if imap == -1:
            raise GSEXRM_Exception("Could not find ROI '%s'" % name)

        return self.xrfmap[dat][:, :, imap]

    def get_map_erange(self, det=None, dtcorrect=True,
                       emin=None, emax=None, by_energy=True):
        """extract map for an ROI set here, by energy range:

        if by_energy is True, emin/emax are taken to be in keV (Energy units)
        otherwise, they are taken to be integer energy channel numbers
        """
        pass

    def get_rgbmap(self, rroi, groi, broi, det=None,
                   dtcorrect=True, scale_each=True, scales=None):
        """return a (NxMx3) array for Red, Green, Blue from named
        ROIs (using get_roimap).

        Arguments
        -----------

        scale_each  scale each map separately to span the full color range. [True]
        scales      if not None and a 3 element tuple, used
                    as the multiplicative scale for each map.               [None]

        By default (scales_each=True, scales=None), each map is scaled by
        1.0/map.max() -- that is 1 of the max value for that map.

        If scales_each=False, each map is scaled by the same value
        (1/max intensity of all maps)

        """
        rmap = self.get_roimap(rroi, det=det, dtcorrect=dtcorrect)
        gmap = self.get_roimap(groi, det=det, dtcorrect=dtcorrect)
        bmap = self.get_roimap(broi, det=det, dtcorrect=dtcorrect)

        if scales is None or len(scales) != 3:
            scales = (1./rmap.max(), 1./gmap.max(), 1./bmap.max())
        if scale_each:
            rmap *= scales[0]
            gmap *= scales[1]
            bmap *= scales[2]
        else:
            scale = min(scales[0], scales[1], scales[2])
            rmap *= scale
            bmap *= scale
            gmap *= scale

        return np.array([rmap, gmap, bmap]).swapaxes(0, 2).swapaxes(0, 1)

def read_xrfmap(filename):
    """read GSE XRM FastMap data from HDF5 file or raw map folder"""
    key = 'filename'
    if os.path.isdir(filename):
        key = 'folder'
    kws = {key: filename}
    return GSEXRM_MapFile(**kws)

def registerLarchPlugin():
    return ('_xrf', {'read_xrfmap': read_xrfmap})


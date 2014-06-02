#!/usr/bin/python
"""
support for netcdf file output files containing MCA spectra
from Epics Mapping Mode with XIA xMXAP electronics
"""
import numpy as np
import time
import h5py
import sys
import os

class XSP3Data(object):
    def __init__(self, npix, ndet, nchan):
        self.firstPixel   = 0
        self.numPixels    = 0
        self.counts       = np.zeros((npix, ndet, nchan), dtype='f8')
        self.realTime     = np.zeros((npix, ndet), dtype='i8')
        self.liveTime     = np.zeros((npix, ndet), dtype='i8')
        self.inputCounts  = np.zeros((npix, ndet), dtype='i4')
        self.outputCounts = np.zeros((npix, ndet), dtype='i4')


def read_xsp3_hdf5(fname, npixels=None, verbose=False):
    # Reads a netCDF file created with the DXP xMAP driver
    # with the netCDF plugin buffers

    clocktick = 12.5e-3
    t0 = time.time()
    h5file = h5py.File(fname, 'r')
    
    root  = h5file['entry/instrument']
    
    counts = root['detector/data']
    ndattr = root['detector/NDAttributes']
    print 'XSP3 DATA SHAPE ', fname, counts.shape
    npix, ndet, nchan = counts.shape
    if npixels is None: npixels = npix
    out = XSP3Data(npixels, ndet, nchan)
    out.numPixels = npixels
    t1 = time.time()
    if npix < npixels:
        out.counts[:npix, :, :]  = counts[:]
    else:
        out.counts = counts[:]

    for i in range(ndet):
        rtime = (ndattr['CHAN%iSCA0' % (i+1)].value * clocktick).astype('i8')
        print "XSP3:   ", rtime.shape, out.realTime.shape, npix, ndet, nchan
        out.realTime[:, i] = rtime
        out.liveTime[:, i] = rtime
        out.inputCounts[:, i]  = out.counts[:, i, :].sum(axis=1)
        out.outputCounts[:, i] = out.inputCounts[:, i]

    h5file.close()
    t2 = time.time()
    if verbose:
        print('   time to read file    = %5.1f ms' % ((t1-t0)*1000))
        print('   time to extract data = %5.1f ms' % ((t2-t1)*1000))
        print('   read %i pixels ' %  npix)
        print('   data shape:    ' ,  out.counts.shape)
    return out

def test_read(fname):
    print( fname,  os.stat(fname))
    fd = read_xsp3_hdf5(fname, verbose=True)
    print(fd.counts.shape)

def registerLarchPlugin():
    return ('_xrf', {'read_xsp3_hdf5': read_xsp3_hdf5})
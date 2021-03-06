#!/usr/bin/env python
"""
XANES Data Viewer and Analysis Tool
"""
import os
import sys
import time
import copy
import numpy as np
np.seterr(all='ignore')

from functools import partial
from collections import OrderedDict
import wx
import wx.lib.agw.flatnotebook as flat_nb
import wx.lib.scrolledpanel as scrolled
import wx.lib.mixins.inspection

from wx.richtext import RichTextCtrl

is_wxPhoenix = 'phoenix' in wx.PlatformInfo

from wxutils import (SimpleText, pack, Button, Popup, HLine, FileSave,
                     Choice, Check, MenuItem, GUIColors, GridPanel, CEN,
                     RCEN, LCEN, FRAMESTYLE, Font)

from larch import Interpreter, Group
from larch.utils import index_of
from larch.utils.strutils import file2groupname, unique_name

from larch.larchlib import read_workdir, save_workdir, read_config, save_config

from larch.wxlib import (LarchPanel, LarchFrame, ColumnDataFileFrame,
                         ReportFrame, BitmapButton, FileCheckList,
                         FloatCtrl, SetTip)

from larch.fitting import fit_report

from larch_plugins.std import group2dict

from larch_plugins.wx.plotter import _newplot, _plot
from larch_plugins.wx.icons import get_icon
from larch_plugins.wx.athena_importer import AthenaImporter

from larch_plugins.xasgui import (PrePeakPanel, XASNormPanel,
                                  MergeDialog, RenameDialog)

from larch_plugins.io import (read_ascii, read_xdi, read_gsexdi,
                              gsescan_group, fix_varname, groups2csv,
                              is_athena_project, AthenaProject)

from larch_plugins.xafs import pre_edge, pre_edge_baseline

LCEN = wx.ALIGN_LEFT|wx.ALIGN_CENTER_VERTICAL
CEN |=  wx.ALL
FILE_WILDCARDS = "Data Files(*.0*,*.dat,*.xdi,*.prj)|*.0*;*.dat;*.xdi;*.prj|All files (*.*)|*.*"
FNB_STYLE = flat_nb.FNB_NO_X_BUTTON|flat_nb.FNB_NODRAG|flat_nb.FNB_NO_NAV_BUTTONS


PLOTOPTS_1 = dict(style='solid', linewidth=3, marker='None', markersize=4)
PLOTOPTS_2 = dict(style='short dashed', linewidth=2, zorder=3,
                  marker='None', markersize=4)
PLOTOPTS_D = dict(style='solid', linewidth=2, zorder=2,
                  side='right',  marker='None', markersize=4)

ICON_FILE = 'larch.ico'

SMOOTH_OPS = ('None', 'Boxcar', 'Savitzky-Golay', 'Convolution')
CONV_OPS  = ('Lorenztian', 'Gaussian')


def assign_gsescan_groups(group):
    labels = group.array_labels
    labels = []
    for i, name in enumerate(group.pos_desc):
        name = fix_varname(name.lower())
        labels.append(name)
        setattr(group, name, group.pos[i, :])

    for i, name in enumerate(group.sums_names):
        name = fix_varname(name.lower())
        labels.append(name)
        setattr(group, name, group.sums_corr[i, :])

    for i, name in enumerate(group.det_desc):
        name = fix_varname(name.lower())
        labels.append(name)
        setattr(group, name, group.det_corr[i, :])

    group.array_labels = labels

class XASController():
    """
    class hollding the Larch session and doing the
    processing work for Larch XAS GUI
    """
    config_file = 'xas_viewer.conf'
    def __init__(self, wxparent=None, _larch=None):
        self.wxparent = wxparent
        self.larch = _larch
        if self.larch is None:
            self.larch = Interpreter()

        self.filelist = None
        self.file_groups = {}
        self.proc_opts = {}
        self.fit_opts = {}
        self.group = None

        self.groupname = None
        self.report_frame = None
        self.symtable = self.larch.symtable

    def init_larch(self):
        fico = self.get_iconfile()

        _larch = self.larch
        _larch.eval("import xafs_plots")
        _larch.symtable._sys.xas_viewer = Group()
        old_config = read_config(self.config_file)

        config = self.make_default_config()
        for sname in config:
            if old_config is not None and sname in old_config:
                val = old_config[sname]
                if isinstance(val, dict):
                    config[sname].update(val)
                else:
                    config[sname] = val

        for key, value in config.items():
            setattr(_larch.symtable._sys.xas_viewer, key, value)
        os.chdir(config['workdir'])

    def make_default_config(self):
        """ default config, probably called on first run of program"""
        config = {'chdir_on_fileopen': True,
                  'workdir': os.getcwd()}
        config['data_proc'] = dict(eshift=0, smooth_op='None',
                                   smooth_conv='Lorentzian',
                                   smooth_c0=2, smooth_c1=1,
                                   smooth_sig=1)
        config['xas_proc'] = dict(e0=0, pre1=-200, pre2=-25,
                                  edge_step=0, nnorm=2, norm1=25,
                                  norm2=-10, nvict=1, auto_step=True,
                                  auto_e0=True, show_e0=True,
                                  xas_op='Normalized',
                                  deconv_form='none', deconv_ewid=0.5)

        config['prepeaks'] = dict(mask_elo=-10, mask_ehi=-5,
                                  fit_emin=-40, fit_emax=0,
                                  yarray='norm')


        return config

    def get_config(self, key, default=None):
        "get configuration setting"
        confgroup = self.larch.symtable._sys.xas_viewer
        return getattr(confgroup, key, default)

    def save_config(self):
        """save configuration"""
        conf = group2dict(self.larch.symtable._sys.xas_viewer)
        conf.pop('__name__')
        # print("Saving configuration: ", self.config_file, conf)
        save_config(self.config_file, conf)

    def set_workdir(self):
        self.larch.symtable._sys.xas_viewer.workdir = os.getcwd()

    def show_report(self, fitresult, evt=None):
        shown = False
        try:
            self.report_frame.Raise()
            shown = True
        except:
            del self.report_frame
        if not shown:
            self.report_frame = ReportFrame(self.wxparent)

        model_repr = fitresult.model._reprstring(long=True)
        report = fit_report(fitresult, show_correl=True,
                            min_correl=0.25, sort_pars=True)

        self.report_frame.SetFont(Font(8))
        self.report_frame.set_text(report)
        self.report_frame.SetFont(Font(8))
        self.report_frame.Raise()

    def get_iconfile(self):
        larchdir = self.symtable._sys.config.larchdir
        return os.path.join(larchdir, 'icons', ICON_FILE)

    def get_display(self, stacked=False):
        win = 1
        wintitle='Larch XAS Plot Window'
        if stacked:
            win = 2
            wintitle='Larch XAS Plot Window'
        opts = dict(wintitle=wintitle, stacked=stacked, win=win,
                    size=(600, 600))
        out = self.symtable._plotter.get_display(**opts)
        return out

    def get_group(self, groupname):
        if groupname is None:
            groupname = self.groupname
        grp = getattr(self.symtable, groupname, None)
        if not hasattr(grp, 'proc_opts'):
            grp.proc_opts = {}
        return grp

    def get_proc_opts(self, dgroup):
        opts = {}
        opts.update(self.get_config('data_proc', default={}))
        if dgroup.datatype == 'xas':
            opts.update(self.get_config('xas_proc', {}))

        if hasattr(dgroup, 'proc_opts'):
            opts.update(dgroup.proc_opts)
        return opts

    def process(self, dgroup, proc_opts=None):
        if not hasattr(dgroup, 'proc_opts'):
            dgroup.proc_opts = {}

        if 'escale' not in dgroup.proc_opts:
            dgroup.proc_opts.update(self.get_proc_opts(dgroup))

        if proc_opts is not None:
            dgroup.proc_opts.update(proc_opts)

        opts = {}
        opts.update(dgroup.proc_opts)
        opts['group'] = dgroup.groupname


        # smoothing
        smop = opts['smooth_op'].lower()
        smcmd = None
        if smop.startswith('box'):
            opts['smooth_c0'] = int(opts['smooth_c0'])
            smcmd = "boxcar({group:s}.ydat, {smooth_c0:d})"
        elif smop.startswith('savit'):
            opts['smooth_c0'] = int(opts['smooth_c0'])
            opts['smooth_c1'] = int(opts['smooth_c1'])
            smcmd = "savitzky_golay({group:s}.ydat, {smooth_c0:d}, {smooth_c1:d})"
        elif smop.startswith('conv'):
            cform = str(opts['smooth_conv'].lower())
            smcmd = "smooth({group:s}.xdat, {group:s}.ydat, sigma={smooth_sig:f}, form='{smooth_conv:s}')"

        if smcmd is not None:
            cmd = "{group:s}.y = " + smcmd
            self.larch.eval(cmd.format(**opts))



        # xas
        if dgroup.datatype.startswith('xas'):

            copts = [dgroup.groupname]
            if not opts['auto_e0']:
                _e0 = opts['e0']
                if _e0 < max(dgroup.energy) and _e0 > min(dgroup.energy):
                    copts.append("e0=%.4f" % float(_e0))

            if not opts['auto_step']:
                copts.append("step=%.4f" % opts['edge_step'])

            for attr in ('pre1', 'pre2', 'nvict', 'nnorm', 'norm1', 'norm2'):
                copts.append("%s=%.4f" % (attr, opts[attr]))

            self.larch.eval("pre_edge(%s)" % (','.join(copts)))

            # deconvolution
            deconv_form = opts['deconv_form'].lower()
            deconv_ewid = float(opts['deconv_ewid'])
            if not deconv_form.startswith('none') and deconv_ewid > 1.e-3:
                cmd = "xas_deconvolve({group:s}, form='{deconv_form:s}', esigma={deconv_ewid:f})"
                self.larch.eval(cmd.format(**opts))

            opts['e0']        = getattr(dgroup, 'e0', dgroup.energy[0])
            opts['edge_step'] = getattr(dgroup, 'edge_step', 1.0)
            for attr in  ('pre1', 'pre2', 'norm1', 'norm2'):
                opts[attr] = getattr(dgroup.pre_edge_details, attr, 0.0)
            dgroup.proc_opts.update(opts)

    def xas_preedge_baseline(self, dgroup, opts=None):
        if not dgroup.datatype.startswith('xas'):
            return

        popts = {'group': dgroup.groupname}
        popts.update(opts)

        copts = [dgroup.groupname]
        copts.append("form='lorentzian'")
        for attr in ('elo', 'ehi', 'emin', 'emax'):
            copts.append("%s=%.4f" % (attr, popts[attr]))
        cmd = "pre_edge_baseline(%s)" % (','.join(copts))
        self.larch.eval(cmd)
        ppeaks = dgroup.prepeaks
        dgroup.centroid_msg = "%.4f +/- %.4f eV" % (ppeaks.centroid,
                                                    ppeaks.delta_centroid)

    def merge_groups(self, grouplist, master=None, yarray='mu', outgroup=None):
        """merge groups"""
        cmd = """%s = merge_groups(%s, master=%s,
        xarray='energy', yarray='%s', kind='cubic', trim=True)"""
        glist = "[%s]" % (', '.join(grouplist))
        outgroup = fix_varname(outgroup.lower())
        if outgroup is None:
            outgroup = 'merged'

        outgroup = unique_name(outgroup, self.file_groups, max=1000)

        cmd = cmd % (outgroup, glist, master, yarray)
        self.larch.eval(cmd)

        if master is None:
            master = grouplist[0]
        this = self.get_group(outgroup)
        master = self.get_group(master)
        this.proc_opts.update(master.proc_opts)
        this.proc_opts['group']  = outgroup
        this.datatype = master.datatype
        this.xdat = 1.0*this.energy
        this.ydat = 1.0*getattr(this, yarray)
        this.plot_xlabel = 'energy'
        this.plot_ylabel = yarray

        return outgroup

    def get_cursor(self):
        try:
            xval = self.symtable._plotter.plot1_x
            yval = self.symtable._plotter.plot1_y
        except:
            xval, yval = None, None
        return xval, yval

    def plot_group(self, groupname=None, title=None,
                   new=True, unzoom=True, use_yarrays=True, **kws):
        # print("## plot_group ", groupname, time.ctime())
        ppanel = self.get_display(stacked=False).panel
        newplot = ppanel.plot
        oplot   = ppanel.oplot
        plotcmd = oplot
        viewlims = ppanel.get_viewlimits()
        if new:
            plotcmd = newplot

        dgroup = self.get_group(groupname)
        if not hasattr(dgroup, 'xdat'):
            print("Cannot plot group ", groupname)

        if dgroup.datatype == 'xas':
            if ((getattr(dgroup, 'plot_yarrays', None) is None or
                 getattr(dgroup, 'energy', None) is None or
                 getattr(dgroup, 'mu', None) is None)):
                self.process(dgroup)

        if use_yarrays and hasattr(dgroup, 'plot_yarrays'):
            plot_yarrays = dgroup.plot_yarrays
        else:
            plot_yarrays = [(dgroup.ydat, {}, None)]

        popts = kws
        path, fname = os.path.split(dgroup.filename)
        if not 'label' in popts:
            popts['label'] = dgroup.plot_ylabel
        unzoom = (unzoom or
                  min(dgroup.xdat) >= viewlims[1] or
                  max(dgroup.xdat) <= viewlims[0] or
                  min(dgroup.ydat) >= viewlims[3] or
                  max(dgroup.ydat) <= viewlims[2])

        if not unzoom:
            popts['xmin'] = viewlims[0]
            popts['xmax'] = viewlims[1]
            popts['ymin'] = viewlims[2]
            popts['ymax'] = viewlims[3]

        popts['xlabel'] = dgroup.plot_xlabel
        popts['ylabel'] = dgroup.plot_ylabel
        if getattr(dgroup, 'plot_y2label', None) is not None:
            popts['y2label'] = dgroup.plot_y2label

        plot_extras = None
        if new:
            if title is None:
                title = fname
            plot_extras = getattr(dgroup, 'plot_extras', None)

        popts['title'] = title
        if hasattr(dgroup, 'custom_plotopts'):
            popts.update(dgroup.custom_plotopts)
        for yarr in plot_yarrays:
            popts.update(yarr[1])
            if yarr[2] is not None:
                popts['label'] = yarr[2]
            plotcmd(dgroup.xdat, yarr[0], **popts)
            plotcmd = oplot

        if plot_extras is not None:
            axes = ppanel.axes
            for etype, x, y, opts in plot_extras:
                if etype == 'marker':
                    popts = {'marker': 'o', 'markersize': 4,
                             'label': '_nolegend_',
                             'markerfacecolor': 'red',
                             'markeredgecolor': '#884444'}
                    popts.update(opts)
                    axes.plot([x], [y], **popts)
                elif etype == 'vline':
                    popts = {'ymin': 0, 'ymax': 1.0,
                             'color': '#888888'}
                    popts.update(opts)
                    axes.axvline(x, **popts)
        ppanel.canvas.draw()


class XASFrame(wx.Frame):
    _about = """Larch XAS GUI: XAS Visualization and Analysis

    Matt Newville <newville @ cars.uchicago.edu>
    """
    def __init__(self, parent=None, size=(925, 675), _larch=None, **kws):
        wx.Frame.__init__(self, parent, -1, size=size, style=FRAMESTYLE)

        self.last_array_sel = {}
        self.paths2read = []

        title = "Larch XAS GUI: XAS Visualization and Analysis"

        self.larch_buffer = parent
        if not isinstance(parent, LarchFrame):
            self.larch_buffer = LarchFrame(_larch=_larch)

        self.larch_buffer.Show()
        self.larch_buffer.Raise()
        self.larch=self.larch_buffer.larchshell
        self.controller = XASController(wxparent=self, _larch=self.larch)
        self.current_filename = None
        self.subframes = {}
        self.plotframe = None
        self.SetTitle(title)
        self.SetSize(size)
        self.SetFont(Font(10))

        self.larch_buffer.Hide()

        self.createMainPanel()
        self.createMenus()
        self.statusbar = self.CreateStatusBar(2, 0)
        self.statusbar.SetStatusWidths([-3, -1])
        statusbar_fields = ["Initializing....", " "]
        for i in range(len(statusbar_fields)):
            self.statusbar.SetStatusText(statusbar_fields[i], i)

    def createMainPanel(self):
        splitter  = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE)
        splitter.SetMinimumPaneSize(250)

        leftpanel = wx.Panel(splitter)
        ltop = wx.Panel(leftpanel)

        def Btn(msg, x, act):
            b = Button(ltop, msg, size=(x, 30),  action=act)
            b.SetFont(Font(10))
            return b

        plot_one = Btn('Plot One',      120, self.onPlotOne)
        plot_sel = Btn('Plot Selected', 120, self.onPlotSel)
        sel_none = Btn('Select None',   120, self.onSelNone)
        sel_all  = Btn('Select All',    120, self.onSelAll)

        self.controller.filelist = FileCheckList(leftpanel, main=self,
                                                 select_action=self.ShowFile,
                                                 remove_action=self.RemoveFile)
        self.controller.filelist.SetBackgroundColour(wx.Colour(255, 255, 255))

        tsizer = wx.GridBagSizer(1, 1)
        tsizer.Add(plot_one, (0, 0), (1, 1), LCEN, 2)
        tsizer.Add(plot_sel, (0, 1), (1, 1), LCEN, 2)
        tsizer.Add(sel_none, (1, 0), (1, 1), LCEN, 2)
        tsizer.Add(sel_all,  (1, 1), (1, 1), LCEN, 2)

        pack(ltop, tsizer)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(ltop, 0, LCEN|wx.GROW, 1)
        sizer.Add(self.controller.filelist, 1, LCEN|wx.GROW|wx.ALL, 1)

        pack(leftpanel, sizer)

        # right hand side
        panel = wx.Panel(splitter)
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.title = SimpleText(panel, 'initializing...', size=(300, -1))
        self.title.SetFont(Font(10))

        ir = 0
        sizer.Add(self.title, 0, LCEN|wx.GROW|wx.ALL, 1)

        self.nb = flat_nb.FlatNotebook(panel, -1, agwStyle=FNB_STYLE)

        self.nb.SetTabAreaColour(wx.Colour(250,250,250))
        self.nb.SetActiveTabColour(wx.Colour(254,254,195))

        self.nb.SetNonActiveTabTextColour(wx.Colour(10,10,128))
        self.nb.SetActiveTabTextColour(wx.Colour(128,0,0))

        panel_opts = dict(parent=self, controller=self.controller)

        self.xasnorm_panel = XASNormPanel(**panel_opts)
        self.prepeak_panel = PrePeakPanel(**panel_opts)

        self.nb.AddPage(self.xasnorm_panel,  ' XAS Normalization ',  True)
        self.nb.AddPage(self.prepeak_panel,   ' Pre-edge Peak Fit ',  True)

        sizer.Add(self.nb, 1, LCEN|wx.EXPAND, 2)
        self.nb.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self.onNBChanged)
        self.nb.SetSelection(0)

        pack(panel, sizer)

        splitter.SplitVertically(leftpanel, panel, 1)
        wx.CallAfter(self.init_larch)

    def onNBChanged(self, event=None):
        idx = self.nb.GetSelection()
        #if idx == 0:   # data processing menu

    def onSelAll(self, event=None):
        self.controller.filelist.SetCheckedStrings(self.controller.file_groups.keys())

    def onSelNone(self, event=None):
        self.controller.filelist.SetCheckedStrings([])

    def init_larch(self):
        self.SetStatusText('initializing Larch')
        self.title.SetLabel('')

        self.prepeak_panel.larch = self.controller.larch

        self.controller.init_larch()

        plotframe = self.controller.get_display(stacked=False)
        xpos, ypos = self.GetPosition()
        xsiz, ysiz = self.GetSize()
        plotframe.SetPosition((xpos+xsiz, ypos))

        self.SetStatusText('ready')
        self.Raise()


    def write_message(self, s, panel=0):
        """write a message to the Status Bar"""
        self.SetStatusText(s, panel)

    def onPlotOne(self, evt=None, groupname=None):
        if groupname is None:
            groupname = self.controller.groupname

        dgroup = self.controller.get_group(groupname)
        if dgroup is not None:
            self.controller.plot_group(groupname=groupname, new=True)

    def onPlotSel(self, evt=None):
        newplot = True
        group_ids = self.controller.filelist.GetCheckedStrings()
        last_id = group_ids[-1]
        for checked in group_ids:
            groupname = self.controller.file_groups[str(checked)]
            dgroup = self.controller.get_group(groupname)
            if dgroup is not None:
                self.controller.plot_group(groupname=groupname, title='',
                                           new=newplot, use_yarrays=False,
                                           label=dgroup.filename,
                                           show_legend=True,
                                           delay_draw=(last_id!=checked))
                newplot=False

    def plot_group(self, groupname=None, title=None, new=True, **kws):
        self.controller.plot_group(groupname=groupname, title=title, new=new, **kws)
        self.Raise()

    def RemoveFile(self, fname=None, **kws):
        if fname is not None:
            s = str(fname)
            if s in self.controller.file_groups:
                group = self.controller.file_groups.pop(s)

    def ShowFile(self, evt=None, groupname=None, **kws):
        filename = None
        if evt is not None:
            filename = str(evt.GetString())
        if groupname is None and filename is not None:
            groupname = self.controller.file_groups[filename]

        if not hasattr(self.larch.symtable, groupname):
            return

        dgroup = self.controller.get_group(groupname)
        self.controller.group = dgroup
        self.controller.groupname = groupname
        self.nb.SetSelection(0)
        self.xasnorm_panel.fill(dgroup)
        self.xasnorm_panel.needs_update = True
        if filename is None:
            filename = dgroup.filename
        self.title.SetLabel(filename)
        self.current_filename = filename

    def createMenus(self):
        # ppnl = self.plotpanel
        self.menubar = wx.MenuBar()
        #
        fmenu = wx.Menu()
        data_menu = wx.Menu()
        ppeak_menu = wx.Menu()
        self.menuitems = items = {}

        items['file_open'] = MenuItem(self, fmenu, "&Open Data File\tCtrl+O",
                                  "Open Data File",  self.onReadDialog)


        items['file2csv'] = MenuItem(self, fmenu, "Export Selected Groups to CSV",
                                     "Export Selected Groups to CSV", self.onData2CSV)

        items['file2athena'] = MenuItem(self, fmenu, "Export Selected Groups to Athena Project",
                                        "Export Selected Groups to Athena Project", self.onData2Athena)

        fmenu.AppendSeparator()

        items['larch_buffer'] = MenuItem(self, fmenu, 'Show Larch Buffer\tCtrl+L',
                                         'Show Larch Programming Buffer',
                                         self.onShowLarchBuffer)

        items['quit'] = MenuItem(self, fmenu, "&Quit\tCtrl+Q", "Quit program", self.onClose)



        items['group_copy'] = MenuItem(self, data_menu, "Copy This Group",
                                         "Copy This Group",
                                         self.onCopyGroup)

        items['group_rename'] = MenuItem(self, data_menu, "Rename This Group",
                                         "Rename This Group",
                                         self.onRenameGroup)

        items['group_remove'] = MenuItem(self, data_menu, "Remove Selected Groups",
                                         "Remove Selected Group",
                                         self.onConfigDataProcessing)


        items['data_merge'] = MenuItem(self, data_menu, "Merge Selected Groups",
                                            "Merge Selected Groups",
                                            self.onMergeData)

        data_menu.AppendSeparator()

        items['data_deglitch'] = MenuItem(self, data_menu, "Deglitch Data",
                                          "Deglitch Data for This Group",
                                          self.onDeglitchData)

        items['data_smooth'] = MenuItem(self, data_menu, "Smooth Data",
                                         "Smooth Data for This Group",
                                         self.onDeglitchData)

        items['data_encalib'] = MenuItem(self, data_menu, "Recalibrate Energy",
                                         "Recalibrate Energy for This Group",
                                         self.onDeglitchData)



        items['fit_readresult'] = MenuItem(self, ppeak_menu,
                                           "&Read Fit Result File\tCtrl+R",
                                           "Read Fit Result File",
                                           self.onReadFitResult)

        items['fit_saveresult'] = MenuItem(self, ppeak_menu,
                                           "Save Fit Result",
                                           "Save Fit Result",
                                           self.onSaveFitResult)

        items['fit_export'] = MenuItem(self, ppeak_menu,
                                       "Export Data and Fit",
                                       "Export Data and Fit",
                                       self.onExportFitResult)


        self.afterfit_menus = ('fit_export', 'fit_saveresult')

        for m in self.afterfit_menus:
            items[m].Enable(False)

        self.menubar.Append(fmenu, "&File")
        self.menubar.Append(data_menu, "Data")
        self.menubar.Append(ppeak_menu, "PreEdge Peaks")
        self.SetMenuBar(self.menubar)
        self.Bind(wx.EVT_CLOSE,  self.onClose)

    def onShowLarchBuffer(self, evt=None):
        if self.larch_buffer is None:
            self.larch_buffer = LarchFrame(_larch=self.larch)
        self.larch_buffer.Show()
        self.larch_buffer.Raise()


    def onData2CSV(self, evt=None):
        group_ids = self.controller.filelist.GetCheckedStrings()
        groups2save = []
        groupnames = []
        for checked in group_ids:
            groupname = self.controller.file_groups[str(checked)]
            dgroup = self.controller.get_group(groupname)
            groups2save.append(dgroup)
            groupnames.append(groupname)
        if len(dgroup) < 1:
            return

        deffile = "%s_%i.csv" % (groupname, len(groupnames))
        wcards  = 'CSV Files (*.csv)|*.cvs|All files (*.*)|*.*'

        outfile = FileSave(self, 'Export Selected Groups to CSV File',
                           default_file=deffile, wildcard=wcards)

        if outfile is None:
            return

        groups2csv(groups2save, outfile, x='energy', y='norm', _larch=self.larch)


    def onData2Athena(self, evt=None):
        group_ids = self.controller.filelist.GetCheckedStrings()
        groups2save = []
        groupnames = []
        for checked in group_ids:
            groupname = self.controller.file_groups[str(checked)]
            dgroup = self.controller.get_group(groupname)
            groups2save.append(dgroup)
            groupnames.append(groupname)
        if len(dgroup) < 1:
            return

        deffile = "%s_%i.prj" % (groupname, len(groupnames))
        wcards  = 'Athena Projects (*.prj)|*.prj|All files (*.*)|*.*'

        outfile = FileSave(self, 'Export Selected Groups to Athena File',
                           default_file=deffile, wildcard=wcards)

        if outfile is None:
            return

        aprj = AthenaProject(filename=outfile, _larch=self.larch)
        for label, grp in zip(groupnames, groups2save):
            aprj.add_group(grp, label=label)
        aprj.save(use_gzip=True)

    def onConfigDataProcessing(self, event=None):
        pass

    def onCopyGroup(self, event=None):
        fname = self.current_filename
        if fname is None:
            fname = self.current_filename = self.controller.filelist.GetStringSelection()

        groupname = self.controller.file_groups[fname]
        if not hasattr(self.larch.symtable, groupname):
            return
        ogroup = self.controller.get_group(groupname)

        ngroup = Group(datatype=ogroup.datatype,
                       energy=1.0*ogroup.energy,
                       mu=1.0*ogroup.mu,
                       xdat=1.0*ogroup.energy,
                       ydat=1.0*ogroup.mu)

        for attr in dir(ogroup):
            if attr in ('i0', 'data' 'yerr'):
                val = getattr(ogroup, attr)*1.0
            if attr in ('norm', 'flat', 'deriv', 'deconv', 'post_edge', 'pre_edge'):
                pass
            else:
                try:
                    val = copy.deepcopy(getattr(ogroup, attr))
                except ValueError:
                    val = None
            setattr(ngroup, attr, val)

        new_fname = unique_name(fname,     self.controller.file_groups.keys())
        new_gname = unique_name(groupname, self.controller.file_groups.values())
        setattr(self.larch.symtable, new_gname, ngroup)
        self.install_group(new_gname, new_fname, overwrite=False)
        self.controller.process(ngroup)
        self.ShowFile(groupname=new_gname)

    def onRenameGroup(self, event=None):
        if fname is None:
            fname = self.current_filename = self.controller.filelist.GetStringSelection()

        dlg = RenameDialog(self, fame)
        res = dlg.GetResponse()
        dlg.Destroy()

        if res.ok:
            groupname = self.controller.file_groups.pop(self.current_filename)
            self.controller.file_groups[res.newname] = groupname
            self.controller.filelist.rename_item(self.current_filename, res.newname)
            dgroup = self.controller.get_group(groupname)
            dgroup.filename = self.current_filename = res.newname
            self.controller.filelist.SetStringSelection(res.newname)

    def onMergeData(self, event=None):
        groups = []
        for checked in self.controller.filelist.GetCheckedStrings():
            groups.append(self.controller.file_groups[str(checked)])
        if len(groups) < 1:
            return

        outgroup = unique_name(outgroup, self.controller.file_groups)
        dlg = MergeDialog(self, groups, outgroup=outgroup)
        res = dlg.GetResponse()
        dlg.Destroy()

        if res.ok:
            yname = 'norm' if res.ynorm else 'mu'
            self.controller.merge_groups(groups, master=res.master,
                                         yarray=yname, outgroup=res.group)
            self.install_group(res.group, res.group, overwrite=False)
            self.controller.filelist.SetStringSelection(res.group)

    def onDeglitchData(self, event=None):
        print(" Deglitch Data")
        pass

    def onConfigDataFitting(self, event=None):
        pass

    # def showInspectionTool(self, event=None):
    #    app = wx.GetApp()
    #    app.ShowInspectionTool()

    def onAbout(self,evt):
        dlg = wx.MessageDialog(self, self._about,
                               "About Larch XAS GUI",
                               wx.OK | wx.ICON_INFORMATION)
        dlg.ShowModal()
        dlg.Destroy()

    def onClose(self, event):
        dlg = wx.MessageDialog(None, 'Really Quit?', 'Question',
                               wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION)

        if wx.ID_YES != dlg.ShowModal():
            return

        self.controller.save_config()
        self.xasnorm_panel.proc_timer.Stop()
        time.sleep(0.05)

        plotframe = self.controller.get_display(stacked=False)
        plotframe.Destroy()

        if self.larch_buffer is not None:
            try:
                self.larch_buffer.Destroy()
            except:
                pass
            time.sleep(0.05)

        for nam in dir(self.larch.symtable._plotter):
            obj = getattr(self.larch.symtable._plotter, nam)
            time.sleep(0.05)
            try:
                obj.Destroy()
            except:
                pass

        for name, wid in self.subframes.items():
            if wid is not None:
                try:
                    wid.Destroy()
                except:
                    pass

        for nam in dir(self.larch.symtable._sys.wx):
            obj = getattr(self.larch.symtable._sys.wx, nam)

        self.Destroy()

    def show_subframe(self, name, frameclass, **opts):
        shown = False
        if name in self.subframes:
            try:
                self.subframes[name].Raise()
                shown = True
            except:
                del self.subframes[name]
        if not shown:
            self.subframes[name] = frameclass(self, **opts)

    def onSelectColumns(self, event=None):
        dgroup = self.controller.get_group(self.controller.groupname)
        self.show_subframe('readfile', ColumnDataFileFrame,
                           group=dgroup.raw,
                           last_array_sel=self.last_array_sel,
                           _larch=self.larch,
                           read_ok_cb=partial(self.onRead_OK,
                                              overwrite=True))

    def onReadFitResult(self, event=None):
        self.prepeak_panel.onLoadFitResult(event=event)

    def onSaveFitResult(self, event=None):
        self.prepeak_panel.onSaveFitResult(event=event)

    def onExportFitResult(self, event=None):
        self.prepeak_panel.onExportFitResult(event=event)

    def onReadDialog(self, event=None):
        dlg = wx.FileDialog(self, message="Read Data File",
                            defaultDir=os.getcwd(),
                            wildcard=FILE_WILDCARDS,
                            style=wx.FD_OPEN|wx.FD_MULTIPLE)
        self.paths2read = []
        if dlg.ShowModal() == wx.ID_OK:
            self.paths2read = dlg.GetPaths()
        dlg.Destroy()

        if len(self.paths2read) < 1:
            return

        path = self.paths2read.pop(0)
        path = path.replace('\\', '/')
        do_read = True
        if path in self.controller.file_groups:
            do_read = (wx.ID_YES == Popup(self,
                                          "Re-read file '%s'?" % path,
                                          'Re-read file?'))
        if do_read:
            self.onRead(path)

    def onRead(self, path):
        filedir, filename = os.path.split(path)
        if self.controller.get_config('chdir_on_fileopen'):
            os.chdir(filedir)
            self.controller.set_workdir()


        # check for athena projects
        if is_athena_project(path):
            kwargs = dict(filename=path,
                          _larch = self.controller.larch,
                          read_ok_cb=self.onReadAthenaProject_OK)
            self.show_subframe('athena_import', AthenaImporter, **kwargs)
        else:
            kwargs = dict(filename=path,
                          _larch=self.larch_buffer.larchshell,
                          last_array_sel = self.last_array_sel,
                          read_ok_cb=self.onRead_OK)

            self.show_subframe('readfile', ColumnDataFileFrame, **kwargs)

    def onReadAthenaProject_OK(self, path, namelist):
        """read groups from a list of groups from an athena project file"""
        self.larch.eval("_prj = read_athena('{path:s}', do_fft=False, do_bkg=False)".format(path=path))

        s = """{group:s} = _prj.{group:s}
        {group:s}.datatype = 'xas'
        {group:s}.xdat = 1.0*{group:s}.energy
        {group:s}.ydat = 1.0*{group:s}.mu*
        {group:s}.yerr = 1.0
        {group:s}.plot_ylabel = 'mu'
        {group:s}.plot_xlabel = 'energy'
        """
        for gname in namelist:
            self.larch.eval(s.format(group=gname))
            self.install_group(gname, gname)
            self.xasnorm_panel.process(gname)
        self.larch.eval("del _prj")


    def onRead_OK(self, script, path, groupname=None, array_sel=None,
                  overwrite=False):
        """ called when column data has been selected and is ready to be used
        overwrite: whether to overwrite the current datagroup, as when
        editing a datagroup
        """
        abort_read = False
        filedir, filename = os.path.split(path)
        if not overwrite and hasattr(self.larch.symtable, groupname):
            newname = file2groupname(filename, symtable=self.larch.symtable)
            msg = """Warning: groupname '%s' already used.
            Will use groupname '%s' instead """  % (groupname, newname)
            dlg = wx.MessageDialog(self, msg, 'Warning',
                                   wx.OK | wx.CANCEL )

            abort_read = (wx.ID_OK != dlg.ShowModal())
            dlg.Destroy()
            groupname = newname

        if abort_read:
            return
        self.larch.eval(script.format(group=groupname, path=path))
        if array_sel is not None:
            self.last_array_sel = array_sel
        self.install_group(groupname, filename, overwrite=overwrite)

        for path in self.paths2read:
            path = path.replace('\\', '/')
            filedir, filename = os.path.split(path)
            gname = file2groupname(filename, symtable=self.larch.symtable)
            self.larch.eval(script.format(group=gname, path=path))
            self.install_group(gname, filename, overwrite=True)

    def install_group(self, groupname, filename, overwrite=False):
        """add groupname / filename to list of available data groups"""

        thisgroup = getattr(self.larch.symtable, groupname)
        thisgroup.groupname = groupname
        thisgroup.filename = filename

        datatype = getattr(thisgroup, 'datatype', 'raw')
        # file /group may already exist in list
        if filename in self.controller.file_groups and not overwrite:
            for i in range(1, 101):
                ftest = "%s (%i)"  % (filename, i)
                if ftest not in self.controller.file_groups:
                    filename = ftest
                    break

        if filename not in self.controller.file_groups:
            self.controller.filelist.Append(filename)
            self.controller.file_groups[filename] = groupname
        self.nb.SetSelection(0)
        self.ShowFile(groupname=groupname)
        self.controller.filelist.SetStringSelection(filename)


class XASViewer(wx.App):
    def __init__(self, **kws):
        wx.App.__init__(self, **kws)

    def run(self):
        self.MainLoop()

    def createApp(self):
        frame = XASFrame()
        frame.Show()
        self.SetTopWindow(frame)

    def OnInit(self):
        self.createApp()
        return True

def initializeLarchPlugin(_larch=None):
    """add XAS Frame to _sys.gui_apps """
    if _larch is not None:
        _sys = _larch.symtable._sys
        if not hasattr(_sys, 'gui_apps'):
            _sys.gui_apps = {}
        _sys.gui_apps['xas_viewer'] = ('XAS Visualization and Analysis', XASFrame)

def registerLarchPlugin():
    return ('_sys.wx', {})

if __name__ == "__main__":
    XASViewer().run()

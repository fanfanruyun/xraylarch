import time
import os
import numpy as np
np.seterr(all='ignore')

from functools import partial
from collections import OrderedDict
import json

import wx
import wx.lib.scrolledpanel as scrolled
import wx.lib.agw.flatnotebook as flat_nb

import wx.dataview as dv

from wxutils import (SimpleText, pack, Button, HLine, Choice, Check,
                     MenuItem, GUIColors, GridPanel, CEN, RCEN, LCEN,
                     FRAMESTYLE, Font, FileSave, FileOpen)

from lmfit import Parameter, Parameters, fit_report
try:
    from lmfit.model import save_modelresult, load_modelresult
    HAS_MODELSAVE = True
except ImportError:
    HAS_MODELSAVE = False

import lmfit.models as lm_models
from lmfit.printfuncs import gformat, CORREL_HEAD

from larch import Group, site_config
from larch.utils import index_of
from larch.utils.jsonutils import encode4js, decode4js

from larch.wxlib import (ReportFrame, BitmapButton, ParameterWidgets,
                         FloatCtrl, SetTip)

from larch_plugins.std import group2dict
from larch_plugins.io.export_modelresult import export_modelresult
from larch_plugins.wx.icons import get_icon
from larch_plugins.wx.parameter import ParameterPanel

LCEN = wx.ALIGN_LEFT|wx.ALIGN_CENTER_VERTICAL
CEN |=  wx.ALL

FNB_STYLE = flat_nb.FNB_NO_X_BUTTON|flat_nb.FNB_NO_NAV_BUTTONS

ModelTypes = ('Peaks', 'General', 'Steps')

ModelChoices = {'steps': ('<Steps Models>', 'Linear Step', 'Arctan Step',
                          'ErrorFunction Step', 'Logistic Step', 'Rectangle'),
                'general': ('<Generalr Models>', 'Constant', 'Linear',
                            'Quadratic', 'Exponential', 'PowerLaw'),
                'peaks': ('<Peak Models>', 'Gaussian', 'Lorentzian',
                          'Voigt', 'PseudoVoigt', 'DampedHarmonicOscillator',
                          'Pearson7', 'StudentsT', 'SkewedGaussian',
                          'Moffat', 'BreitWigner', 'Donaich', 'Lognormal'),
                }

FitMethods = ("Levenberg-Marquardt", "Nelder-Mead", "Powell")

MIN_CORREL = 0.0010

class FitResultFrame(wx.Frame):
    def __init__(self, parent=None, controller=None, datagroup=None, **kws):

        wx.Frame.__init__(self, None, -1, title='Fit Results',
                          style=FRAMESTYLE, size=(600, 675), **kws)
        self.parent = parent
        self.controller = controller
        self.larch = controller.larch
        self.datagroup = datagroup
        self.build()
        self.show()

    def build(self):
        sizer = wx.GridBagSizer(10, 5)
        sizer.SetVGap(2)
        sizer.SetHGap(2)

        panel = scrolled.ScrolledPanel(self)
        self.SetMinSize((600, 450))
        self.colors = GUIColors()

        # title row
        self.wids = wids = {}
        title = SimpleText(panel, 'Fit Results',  font=Font(12),
                           colour=self.colors.title, style=LCEN)

        wids['data_title'] = SimpleText(panel, '< > ',  font=Font(12),
                                             colour=self.colors.title, style=LCEN)

        wids['hist_tag'] = SimpleText(panel, 'Fit #1',  font=Font(12),
                                      colour=self.colors.title, style=LCEN)

        wids['hist_info'] = SimpleText(panel, ' ___ ',  font=Font(12),
                                       colour=self.colors.title, style=LCEN)

        sizer.Add(title,              (0, 0), (1, 2), LCEN)
        sizer.Add(wids['data_title'], (0, 2), (1, 2), LCEN)
        sizer.Add(wids['hist_tag'],   (0, 4), (1, 1), LCEN)
        sizer.Add(wids['hist_info'],  (0, 5), (1, 1), LCEN)

        irow = 1
        wids['model_desc'] = SimpleText(panel, '<Model>',  font=Font(12))
        sizer.Add(wids['model_desc'],  (irow, 0), (1, 5), LCEN)

        irow += 1
        sizer.Add(HLine(panel, size=(400, 3)), (irow, 0), (1, 5), LCEN)

        irow += 1
        title = SimpleText(panel, '[[Fit Statistics]]',  font=Font(12),
                           colour=self.colors.title, style=LCEN)
        sizer.Add(title, (irow, 0), (1, 4), LCEN)

        for label, attr in (('Fit method', 'method'),
                            ('# Fit Evaluations', 'nfev'),
                            ('# Data Points', 'ndata'),
                            ('# Fit Variables', 'nvarys'),
                            ('# Free Points', 'nfree'),
                            ('Chi-square', 'chisqr'),
                            ('Reduced Chi-square', 'redchi'),
                            ('Akaike Info Criteria', 'aic'),
                            ('Bayesian Info Criteria', 'bic')):
            irow += 1
            wids[attr] = SimpleText(panel, '?')
            sizer.Add(SimpleText(panel, " %s = " % label),  (irow, 0), (1, 1), LCEN)
            sizer.Add(wids[attr],                           (irow, 1), (1, 1), LCEN)

        irow += 1
        sizer.Add(HLine(panel, size=(400, 3)), (irow, 0), (1, 5), LCEN)

        irow += 1
        title = SimpleText(panel, '[[Variables]]',  font=Font(12),
                           colour=self.colors.title, style=LCEN)
        sizer.Add(title, (irow, 0), (1, 1), LCEN)

        self.wids['copy_params'] = Button(panel, 'Update Model with Best Fit Values',
                                          size=(250, -1), action=self.onCopyParams)

        sizer.Add(self.wids['copy_params'], (irow, 1), (1, 3), LCEN)

        dvstyle = dv.DV_SINGLE|dv.DV_VERT_RULES|dv.DV_ROW_LINES
        pview = self.wids['params'] = dv.DataViewListCtrl(panel, style=dvstyle)
        self.wids['paramsdata'] = []
        pview.AppendTextColumn('Parameter',         width=150)
        pview.AppendTextColumn('Best-Fit Value',    width=100)
        pview.AppendTextColumn('Standard Error',    width=100)
        pview.AppendTextColumn('Info ',             width=275)

        for col in (0, 1, 2, 3):
            this = pview.Columns[col]
            isort, align = True, wx.ALIGN_LEFT
            if col in (1, 2):
                isort, align = False, wx.ALIGN_RIGHT
            this.Sortable = isort
            this.Alignment = this.Renderer.Alignment = align

        pview.SetMinSize((650, 200))
        pview.Bind(dv.EVT_DATAVIEW_SELECTION_CHANGED, self.onSelectParameter)

        irow += 1
        sizer.Add(pview, (irow, 0), (1, 5), LCEN)

        irow += 1
        sizer.Add(HLine(panel, size=(400, 3)), (irow, 0), (1, 5), LCEN)

        irow += 1
        title = SimpleText(panel, '[[Correlations]]',  font=Font(12),
                           colour=self.colors.title, style=LCEN)

        self.wids['all_correl'] = Button(panel, 'Show All',
                                          size=(100, -1), action=self.onAllCorrel)

        self.wids['min_correl'] = FloatCtrl(panel, value=MIN_CORREL,
                                            minval=0, size=(60, -1), gformat=True)

        ctitle = SimpleText(panel, 'minimum correlation: ')
        sizer.Add(title,  (irow, 0), (1, 1), LCEN)
        sizer.Add(ctitle, (irow, 1), (1, 1), LCEN)
        sizer.Add(self.wids['min_correl'], (irow, 2), (1, 1), LCEN)
        sizer.Add(self.wids['all_correl'], (irow, 3), (1, 1), LCEN)

        irow += 1

        cview = self.wids['correl'] = dv.DataViewListCtrl(panel, style=dvstyle)

        cview.AppendTextColumn('Parameter 1',    width=150)
        cview.AppendTextColumn('Parameter 2',    width=150)
        cview.AppendTextColumn('Correlation',    width=100)

        for col in (0, 1, 2):
            this = cview.Columns[col]
            isort, align = True, wx.ALIGN_LEFT
            if col == 1:
                isort = False
            if col == 2:
                align = wx.ALIGN_RIGHT
            this.Sortable = isort
            this.Alignment = this.Renderer.Alignment = align
        cview.SetMinSize((450, 200))

        irow += 1
        sizer.Add(cview, (irow, 0), (1, 5), LCEN)
        irow += 1
        sizer.Add(HLine(panel, size=(400, 3)), (irow, 0), (1, 5), LCEN)

        pack(panel, sizer)
        panel.SetupScrolling()

        mainsizer = wx.BoxSizer(wx.VERTICAL)
        mainsizer.Add(panel, 1, wx.GROW|wx.ALL, 1)

        pack(self, mainsizer)
        self.Show()
        self.Raise()

    def onSelectParameter(self, evt=None):
        if self.wids['params'] is None:
            return
        if not self.wids['params'].HasSelection():
            return
        item = self.wids['params'].GetSelectedRow()
        pname = self.wids['paramsdata'][item]

        cormin= self.wids['min_correl'].GetValue()
        self.wids['correl'].DeleteAllItems()

        fit_history = getattr(self.datagroup, 'fit_history', [])
        result = fit_history[-1]
        this = result.params[pname]
        if this.correl is not None:
            sort_correl = sorted(this.correl.items(), key=lambda it: abs(it[1]))
            for name, corval in reversed(sort_correl):
                if abs(corval) > cormin:
                    self.wids['correl'].AppendItem((pname, name, "% .4f" % corval))

    def onAllCorrel(self, evt=None):
        fit_history = getattr(self.datagroup, 'fit_history', [])
        params = fit_history[-1].params
        parnames = list(params.keys())

        cormin= self.wids['min_correl'].GetValue()
        correls = {}
        for i, name in enumerate(parnames):
            par = params[name]
            if not par.vary:
                continue
            if hasattr(par, 'correl') and par.correl is not None:
                # print(par, par.correl)
                for name2 in parnames[i+1:]:
                    if (name != name2 and name2 in par.correl and
                            abs(par.correl[name2]) > cormin):
                        correls["%s$$%s" % (name, name2)] = par.correl[name2]

        sort_correl = sorted(correls.items(), key=lambda it: abs(it[1]))
        sort_correl.reverse()

        self.wids['correl'].DeleteAllItems()

        for namepair, corval in sort_correl:
            name1, name2 = namepair.split('$$')
            self.wids['correl'].AppendItem((name1, name2, "% .4f" % corval))

    def onCopyParams(self, evt=None):
        fit_history = getattr(self.datagroup, 'fit_history', [])
        self.parent.fit_panel.update_start_values(fit_history[-1])

    def show(self, datagroup=None):
        if datagroup is not None:
            self.datagroup = datagroup

        fit_history = getattr(self.datagroup, 'fit_history', [])
        if len(fit_history) < 1:
            print("No fit reults to show for datagroup ", self.datagroup)
        result = fit_history[-1]
        wids = self.wids
        wids['method'].SetLabel(result.method)
        wids['ndata'].SetLabel("%d" % result.ndata)
        wids['nvarys'].SetLabel("%d" % result.nvarys)
        wids['nfree'].SetLabel("%d" % result.nfree)
        wids['nfev'].SetLabel("%d" % result.nfev)
        wids['redchi'].SetLabel("%f" % result.redchi)
        wids['chisqr'].SetLabel("%f" % result.chisqr)
        wids['aic'].SetLabel("%f" % result.aic)
        wids['bic'].SetLabel("%f" % result.bic)
        wids['hist_info'].SetLabel("%d" % len(fit_history))
        wids['hist_tag'].SetLabel("Latest Fit") #

        wids['data_title'].SetLabel(self.datagroup.filename)

        wids['model_desc'].SetLabel(result.model_repr)
        wids['params'].DeleteAllItems()
        wids['paramsdata'] = []
        for i, param in enumerate(result.params.values()):
            pname = param.name
            try:
                val = gformat(param.value)
            except (TypeError, ValueError):
                val = ' ??? '

            serr = ' N/A '
            if param.stderr is not None:
                serr = gformat(param.stderr, length=9)

            extra = ' '
            if param.expr is not None:
                extra = ' = %s ' % param.expr
            elif param.init_value is not None:
                extra = ' (init=% .7g)' % param.init_value
            elif not param.vary:
                extra = ' (fixed)'

            wids['params'].AppendItem((pname, val, serr, extra))
            wids['paramsdata'].append(pname)

        self.Refresh()

class PrePeakPanel(wx.Panel):
    def __init__(self, parent=None, controller=None, **kws):

        wx.Panel.__init__(self, parent, -1, size=(550, 625), **kws)
        self.parent = parent
        self.controller = controller
        self.larch = controller.larch
        self.fit_components = OrderedDict()
        self.fit_model = None
        self.fit_params = None
        self.user_added_params = None
        self.summary = None
        self.sizer = wx.GridBagSizer(10, 6)
        self.build_display()
        self.pick2_timer = wx.Timer(self)
        self.pick2_group = None
        self.Bind(wx.EVT_TIMER, self.onPick2Timer, self.pick2_timer)
        self.pick2_t0 = 0.
        self.pick2_timeout = 15.

        self.pick2erase_timer = wx.Timer(self)
        self.pick2erase_panel = None
        self.Bind(wx.EVT_TIMER, self.onPick2EraseTimer, self.pick2erase_timer)

    def build_display(self):

        self.mod_nb = flat_nb.FlatNotebook(self, -1, agwStyle=FNB_STYLE)
        self.mod_nb.SetTabAreaColour(wx.Colour(250,250,250))
        self.mod_nb.SetActiveTabColour(wx.Colour(254,254,195))

        self.mod_nb.SetNonActiveTabTextColour(wx.Colour(10,10,128))
        self.mod_nb.SetActiveTabTextColour(wx.Colour(128,0,0))
        self.mod_nb.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self.onNBChanged)

        pan = self.panel = GridPanel(self, ncols=4, nrows=4, pad=2, itemstyle=LCEN)

        self.btns = {}
        for name in ('ppeak_elo', 'ppeak_emin', 'ppeak_emax', 'ppeak_ehi'):
            bb = BitmapButton(pan, get_icon('plus'),
                              action=partial(self.on_selpoint, opt=name),
                              tooltip='use last point selected from plot')
            self.btns[name] = bb

        opts = dict(size=(65, -1), gformat=True, precision=1,
                    # action=self.UpdatePlot,
                    )

        self.ppeak_emin = FloatCtrl(pan, value=-30, **opts)
        self.ppeak_emax = FloatCtrl(pan, value=0, **opts)
        self.ppeak_elo = FloatCtrl(pan, value=-15, **opts)
        self.ppeak_ehi = FloatCtrl(pan, value=-5, **opts)

        self.ppeak_bkgfit = Button(pan, 'Fit Pre-edge Baseline', size=(175, 30),
                                   action=self.onPreedgeBaseline)

        self.model_type = Choice(pan, size=(100, -1),
                                 choices=ModelTypes,
                                 action=self.onModelTypes)

        self.model_func = Choice(pan, size=(200, -1),
                                 choices=ModelChoices['peaks'],
                                 action=self.addModel)

        pan.Add(SimpleText(pan, 'Fit Energy Range: '), newrow=True)
        pan.Add(self.btns['ppeak_emin'])
        pan.Add(self.ppeak_emin)
        pan.Add(SimpleText(pan, ':'))
        pan.Add(self.btns['ppeak_emax'])
        pan.Add(self.ppeak_emax)

        t = SimpleText(pan, 'Pre-edge Peak Range: ')
        t.SetToolTip('Range used as mask for background')

        pan.Add(t, newrow=True)
        pan.Add(self.btns['ppeak_elo'])
        pan.Add(self.ppeak_elo)
        pan.Add(SimpleText(pan, ':'))
        pan.Add(self.btns['ppeak_ehi'])
        pan.Add(self.ppeak_ehi)
        pan.Add(self.ppeak_bkgfit)

        pan.Add(SimpleText(pan, ' Add Model Type: '), newrow=True)
        pan.Add(self.model_type, dcol=3)
        pan.Add(SimpleText(pan, ' Model: '), dcol=2)
        pan.Add(self.model_func)


        pan.pack()

#         rsizer.Add(SimpleText(range_row, 'Fit Range X=[ '), 0, LCEN, 3)
#         rsizer.Add(xmin_sel, 0, LCEN, 3)
#         rsizer.Add(self.xmin, 0, LCEN, 3)
#         rsizer.Add(SimpleText(range_row, ' : '), 0, LCEN, 3)
#         rsizer.Add(xmax_sel, 0, LCEN, 3)
#         rsizer.Add(self.xmax, 0, LCEN, 3)
#         rsizer.Add(SimpleText(range_row, ' ]  '), 0, LCEN, 3)
#         rsizer.Add(Button(range_row, 'Full Data Range', size=(150, -1),
#                           action=self.onResetRange), 0, LCEN, 3)
#          pack(range_row, rsizer)


#         self.plot_comps = Check(pan, label='Plot Components?',
#                                 default=True, size=(150, -1))
#
#         rsizer.Add(Button(a, 'Run Fit',
#                           size=(100, -1), action=self.onRunFit), 0, RCEN, 3)
#         self.savebtn = Button(action_row, 'Save Fit',
#                               size=(100, -1), action=self.onSaveFitResult)
#         self.savebtn.Disable()
#         rsizer.Add(self.savebtn, 0, LCEN, 3)
#
#         rsizer.Add(Button(action_row, 'Plot Current Model',
#                           size=(175, -1), action=self.onShowModel), 0, LCEN, 3)
#         rsizer.Add(self.plot_comps, 0, LCEN, 3)
#
#         pack(action_row, rsizer)
#

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.AddMany([((10, 10), 0, LCEN, 10), (pan,      0, LCEN, 10),
                       ((10, 10), 0, LCEN, 10),
                       (HLine(self, size=(550, 3)), 0, LCEN, 4),
                       ((10,10), 0, LCEN, 2),
                       (self.mod_nb,  1, LCEN|wx.GROW, 10)])

        pack(self, sizer)

    def onPreedgeBaseline(self, evt=None):
        print(" on preedge baseline")


    def onNBChanged(self, event=None):
        idx = self.mod_nb.GetSelection()

    def onModelTypes(self, event=None):
        modtype = event.GetString().lower()
        self.model_func.SetChoices(ModelChoices[modtype])

    def addModel(self, event=None, model=None):
        if model is None and event is not None:
            model = event.GetString()
        if model is None or model.startswith('<'):
            return

        p = model[0].lower()
        curmodels = ["%s%i_" % (p, i+1) for i in range(1+len(self.fit_components))]
        for comp in self.fit_components:
            if comp in curmodels:
                curmodels.remove(comp)

        prefix = curmodels[0]

        label = "%s(prefix='%s')" % (model, prefix)
        title = "%s: %s" % (prefix[:-1], (model+' '*8)[:8])
        mclass_kws = {'prefix': prefix}
        if 'step' in model.lower():
            form = model.lower().replace('step', '').strip()

            if form.startswith('err'): form = 'erf'
            label = "Step(form='%s', prefix='%s')" % (form, prefix)
            title = "%s: Step %s" % (prefix[:-1], form[:3])
            mclass = lm_models.StepModel
            mclass_kws['form'] = form
            minst = mclass(form=form, prefix=prefix)
        else:
            mclass = getattr(lm_models, model+'Model')
            minst = mclass(prefix=prefix)

        panel = GridPanel(self.mod_nb, ncols=1, nrows=1, pad=1, itemstyle=CEN)

        def SLabel(label, size=(80, -1), **kws):
            return  SimpleText(panel, label,
                               size=size, style=wx.ALIGN_LEFT, **kws)
        usebox = Check(panel, default=True, label='Use in Fit?', size=(150, -1))
        bkgbox = Check(panel, default=False, label='Is Background?', size=(150, -1))

        delbtn = Button(panel, 'Delete Component', size=(150, -1),
                        action=partial(self.onDeleteComponent, prefix=prefix))

        pick2msg = SimpleText(panel, "    ", size=(125, -1))
        pick2btn = Button(panel, 'Pick Values from Data', size=(200, -1),
                          action=partial(self.onPick2Points, prefix=prefix))

        # SetTip(mname,  'Label for the model component')
        SetTip(usebox,   'Use this component in fit?')
        SetTip(bkgbox,   'Label this component as "background" when plotting?')
        SetTip(delbtn,   'Delete this model component')
        SetTip(pick2btn, 'Select X range on Plot to Guess Initial Values')

        panel.Add(SLabel(label, size=(275, -1), colour='#0000AA'),
                  dcol=3,  style=wx.ALIGN_LEFT, newrow=True)
        panel.Add(usebox, dcol=2)
        panel.Add(bkgbox, dcol=2)

        panel.Add(pick2btn, dcol=3, style=wx.ALIGN_LEFT, newrow=True)
        panel.Add(pick2msg, dcol=3, style=wx.ALIGN_RIGHT)
        panel.Add(delbtn, style=wx.ALIGN_LEFT)

        # panel.Add((10, 10), newrow=True)
        # panel.Add(HLine(panel, size=(150,  3)), dcol=4, style=wx.ALIGN_CENTER)

        panel.Add(SLabel("Parameter "), style=wx.ALIGN_LEFT,  newrow=True)
        panel.AddMany((SLabel(" Value"), SLabel(" Type"), SLabel(' Bounds'),
                       SLabel("  Min", size=(60, -1)),
                       SLabel("  Max", size=(60, -1)),
                       SLabel("  Expression")))

        parwids = OrderedDict()
        parnames = sorted(minst.param_names)

        for a in minst._func_allargs:
            pname = "%s%s" % (prefix, a)
            if (pname not in parnames and
                a in minst.param_hints and
                a not in minst.independent_vars):
                parnames.append(pname)

        for pname in parnames:
            sname = pname[len(prefix):]
            hints = minst.param_hints.get(sname, {})

            par = Parameter(name=pname, value=0, vary=True)
            if 'min' in hints:
                par.min = hints['min']
            if 'max' in hints:
                par.max = hints['max']
            if 'value' in hints:
                par.value = hints['value']
            if 'expr' in hints:
                par.expr = hints['expr']

            pwids = ParameterWidgets(panel, par, name_size=100, expr_size=175,
                                     float_size=80, prefix=prefix,
                                     widgets=('name', 'value',  'minval',
                                              'maxval', 'vary', 'expr'))
            parwids[par.name] = pwids
            panel.Add(pwids.name, newrow=True)

            panel.AddMany((pwids.value, pwids.vary, pwids.bounds,
                           pwids.minval, pwids.maxval, pwids.expr))

        for sname, hint in minst.param_hints.items():
            pname = "%s%s" % (prefix, sname)
            if 'expr' in hint and pname not in parnames:
                par = Parameter(name=pname, value=0, expr=hint['expr'])

                pwids = ParameterWidgets(panel, par, name_size=100, expr_size=400,
                                         float_size=80, prefix=prefix,
                                         widgets=('name', 'value', 'expr'))
                parwids[par.name] = pwids
                panel.Add(pwids.name, newrow=True)
                panel.Add(pwids.value)
                panel.Add(pwids.expr, dcol=5, style=wx.ALIGN_RIGHT)
                pwids.value.Disable()


        # panel.Add(delbtn, dcol=2)
        # panel.Add(HLine(panel, size=(250, 3)), dcol=3, style=wx.ALIGN_CENTER)

        fgroup = Group(prefix=prefix, title=title, mclass=mclass,
                       mclass_kws=mclass_kws, usebox=usebox, panel=panel,
                       parwids=parwids, float_size=65, expr_size=150,
                       pick2_msg=pick2msg)

        self.fit_components[prefix] = fgroup
        panel.pack()

        self.mod_nb.AddPage(panel, title, True)
        sx,sy = self.GetSize()
        self.SetSize((sx, sy+1))
        self.SetSize((sx, sy))

    def onDeleteComponent(self, evt=None, prefix=None):
        fgroup = self.fit_components.get(prefix, None)
        if fgroup is None:
            return

        for i in range(self.mod_nb.GetPageCount()):
            if fgroup.title == self.mod_nb.GetPageText(i):
                self.mod_nb.DeletePage(i)

        for attr in dir(fgroup):
            setattr(fgroup, attr, None)

        self.fit_components.pop(prefix)

        sx,sy = self.GetSize()
        self.SetSize((sx, sy+1))
        self.SetSize((sx, sy))

    def onPick2EraseTimer(self, evt=None):
        """erases line trace showing automated 'Pick 2' guess """
        self.pick2erase_timer.Stop()
        panel = self.pick2erase_panel
        ntrace = panel.conf.ntrace - 1
        trace = panel.conf.get_mpl_line(ntrace)
        panel.conf.get_mpl_line(ntrace).set_data(np.array([]), np.array([]))
        panel.conf.ntrace = ntrace
        panel.draw()

    def onPick2Timer(self, evt=None):
        """checks for 'Pick 2' events, and initiates 'Pick 2' guess
        for a model from the selected data range
        """
        try:
            plotframe = self.controller.get_display(stacked=False)
            curhist = plotframe.cursor_hist[:]
            plotframe.Raise()
        except:
            return

        if (time.time() - self.pick2_t0) > self.pick2_timeout:
            msg = self.pick2_group.pick2_msg.SetLabel(" ")
            plotframe.cursor_hist = []
            self.pick2_timer.Stop()
            return

        if len(curhist) < 2:
            self.pick2_group.pick2_msg.SetLabel("%i/2" % (len(curhist)))
            return

        self.pick2_group.pick2_msg.SetLabel("done.")
        self.pick2_timer.Stop()

        # guess param values
        xcur = (curhist[0][0], curhist[1][0])
        xmin, xmax = min(xcur), max(xcur)

        dgroup = getattr(self.larch.symtable, self.controller.groupname)
        x, y = dgroup.x, dgroup.y
        i0 = index_of(dgroup.x, xmin)
        i1 = index_of(dgroup.x, xmax)
        x, y = dgroup.x[i0:i1+1], dgroup.y[i0:i1+1]

        mod = self.pick2_group.mclass(prefix=self.pick2_group.prefix)
        parwids = self.pick2_group.parwids
        try:
            guesses = mod.guess(y, x=x)
        except:
            return

        for name, param in guesses.items():
            if name in parwids:
                parwids[name].value.SetValue(param.value)

        dgroup._tmp = mod.eval(guesses, x=dgroup.x)
        plotframe = self.controller.get_display(stacked=False)
        plotframe.cursor_hist = []
        plotframe.oplot(dgroup.x, dgroup._tmp)
        self.pick2erase_panel = plotframe.panel

        self.pick2erase_timer.Start(5000)


    def onPick2Points(self, evt=None, prefix=None):
        fgroup = self.fit_components.get(prefix, None)
        if fgroup is None:
            return

        plotframe = self.controller.get_display(stacked=False)
        plotframe.Raise()

        plotframe.cursor_hist = []
        fgroup.npts = 0
        self.pick2_group = fgroup

        if fgroup.pick2_msg is not None:
            fgroup.pick2_msg.SetLabel("0/2")

        self.pick2_t0 = time.time()
        self.pick2_timer.Start(250)

    def onSaveFitResult(self, event=None):
        dgroup = self.get_datagroup()
        deffile = dgroup.filename.replace('.', '_') + '.fitresult'
        wcards = 'Fit Results(*.fitresult)|*.fitresult|All files (*.*)|*.*'

        outfile = FileSave(self, 'Save Fit Result',
                           default_file=deffile,
                           wildcard=wcards)

        if outfile is not None:
            try:
                save_modelresult(dgroup.fit_history[-1], outfile)
            except IOError:
                print('could not write %s' % outfile)

    def onLoadFitResult(self, event=None):

        wcards = 'Fit Results(*.fitresult)|*.fitresult|All files (*.*)|*.*'

        mfile = FileOpen(self, 'Load Fit Result',
                         default_file='', wildcard=wcards)
        model = None

        if mfile is not None:
            try:
                model = load_modelresult(mfile)
            except IOError:
                print('could not read model result %s' % mfile)
                return
        if model is None:
            return
        print(" Loading Model (work in progress) ", model)

    def onExportFitResult(self, event=None):
        dgroup = self.get_datagroup()
        deffile = dgroup.filename.replace('.', '_') + '_result.xdi'
        wcards = 'All files (*.*)|*.*'

        outfile = FileSave(self, 'Export Fit Result',
                           default_file=deffile,
                           wildcard=wcards)

        if outfile is None:
            return

        dgroup = self.get_datagroup()

        i1, i2, xv1, xv2 = self.get_xranges(dgroup.x)
        x = dgroup.x[slice(i1, i2)]
        y = dgroup.y[slice(i1, i2)]
        yerr = None
        if hasattr(dgroup, 'yerr'):
            yerr = dgroup.yerr
            if not isinstance(yerr, np.ndarray):
                yerr = yerr * np.ones(len(y))
            else:
                yerr = yerr[slice(i1, i2)]

        export_modelresult(dgroup.fit_history[-1], filename=outfile,
                           datafile=dgroup.filename,
                           ydata=y, yerr=yerr, x=x)


    def onResetRange(self, event=None):
        dgroup = self.get_datagroup()
        self.xmin.SetValue(min(dgroup.x))
        self.xmax.SetValue(max(dgroup.x))

    def on_selpoint(self, evt=None, opt='xmin'):
        xval = None
        try:
            xval = self.larch.symtable._plotter.plot1_x
        except:
            xval = None
        if xval is not None:
            if opt == 'xmin':
                self.xmin.SetValue(xval)
            elif opt == 'xmax':
                self.xmax.SetValue(xval)

    def get_datagroup(self):
        dgroup = None
        if self.controller.groupname is not None:
            try:
                dgroup = getattr(self.larch.symtable,
                                 self.controller.groupname)
            except:
                pass
        return dgroup

    def get_xranges(self, x):
        xmin, xmax = min(x), max(x)
        i1, i2 = 0, len(x)
        _xmin = self.xmin.GetValue()
        _xmax = self.xmax.GetValue()
        if _xmin > min(x):
            i1 = index_of(x, _xmin)
            xmin = x[i1]
        if _xmax < max(x):
            i2 = index_of(x, _xmax) + 1
            xmax = x[i2]
        xv1 = max(min(x), xmin - (xmax-xmin)/5.0)
        xv2 = min(max(x), xmax + (xmax-xmin)/5.0)
        return i1, i2, xv1, xv2

    def build_fitmodel(self):
        """ use fit components to build model"""
        dgroup = self.get_datagroup()
        fullmodel = None
        params = Parameters()
        self.summary = {'components': [], 'options': {}}
        for comp in self.fit_components.values():
            if comp.usebox is not None and comp.usebox.IsChecked():
                for parwids in comp.parwids.values():
                    params.add(parwids.param)
                self.summary['components'].append((comp.mclass.__name__, comp.mclass_kws))
                thismodel = comp.mclass(**comp.mclass_kws)
                if fullmodel is None:
                   fullmodel = thismodel
                else:
                    fullmodel += thismodel

        self.fit_model = fullmodel
        self.fit_params = params

        if dgroup is not None:
            i1, i2, xv1, xv2 = self.get_xranges(dgroup.x)
            xsel = dgroup.x[slice(i1, i2)]
            dgroup.xfit = xsel
            dgroup.yfit = self.fit_model.eval(self.fit_params, x=xsel)
            dgroup.ycomps = self.fit_model.eval_components(params=self.fit_params,
                                                           x=xsel)
        return dgroup

    def onShowModel(self, event=None):
        dgroup = self.build_fitmodel()
        if dgroup is not None:
            with_components = (self.plot_comps.IsChecked() and
                               len(dgroup.ycomps) > 1)

            self.plot_fitmodel(dgroup, show_resid=False,
                               with_components=with_components)

    def plot_fitmodel(self, dgroup, show_resid=False, with_components=None):
        if dgroup is None:
            return
        i1, i2, xv1, xv2 = self.get_xranges(dgroup.x)
        ysel = dgroup.y[slice(i1, i2)]

        plotframe = self.controller.get_display(stacked=True)
        plotframe.plot(dgroup.xfit, ysel, new=True, panel='top',
                       xmin=xv1, xmax=xv2, label='data',
                       xlabel=dgroup.plot_xlabel, ylabel=dgroup.plot_ylabel,
                       title='Fit: %s' % dgroup.filename )

        plotframe.oplot(dgroup.xfit, dgroup.yfit, label='fit')

        plotframe.plot(dgroup.xfit, ysel-dgroup.yfit, grid=False,
                       marker='o', markersize=4, linewidth=1, panel='bot')

        if with_components is None:
            with_components = (self.plot_comps.IsChecked() and
                               len(dgroup.ycomps) > 1)
        if with_components:
            for label, _y in dgroup.ycomps.items():
                plotframe.oplot(dgroup.xfit, _y, label=label,
                                style='short dashed')

        line_opts = dict(color='#AAAAAA', label='_nolegend_',
                    linewidth=1, zorder=-5)
        plotframe.panel_bot.axes.axhline(0, **line_opts)
        axvline = plotframe.panel.axes.axvline
        if i1 > 0:
            axvline(dgroup.x[i1], **line_opts)

        if i2 < len(dgroup.x):
            axvline(dgroup.x[i2-1], **line_opts)

        plotframe.panel.canvas.draw()


    def onRunFit(self, event=None):
        dgroup = self.build_fitmodel()
        if dgroup is None:
            return
        i1, i2, xv1, xv2 = self.get_xranges(dgroup.x)
        dgroup.xfit = dgroup.x[slice(i1, i2)]
        ysel = dgroup.y[slice(i1, i2)]
        weights = np.ones(len(ysel))

        if hasattr(dgroup, 'yerr'):
            yerr = dgroup.yerr
            if not isinstance(yerr, np.ndarray):
                yerr = yerr * np.ones(len(ysel))
            else:
                yerr = yerr[slice(i1, i2)]
            yerr_min = 1.e-9*ysel.mean()
            yerr[np.where(yerr < yerr_min)] = yerr_min
            weights = 1.0/yerr

        result = self.fit_model.fit(ysel, params=self.fit_params,
                                    x=dgroup.xfit, weights=weights,
                                    method='leastsq')
        self.summary['xmin'] = xv1
        self.summary['xmax'] = xv2
        for attr in ('aic', 'bic', 'chisqr', 'redchi', 'ci_out', 'covar',
                     'flatchain', 'success', 'nan_policy', 'nfev', 'ndata',
                     'nfree', 'nvarys', 'init_values'):
            self.summary[attr] = getattr(result, attr)
        self.summary['params'] = result.params


        dgroup.yfit = result.best_fit
        dgroup.ycomps = self.fit_model.eval_components(params=result.params,
                                                       x=dgroup.xfit)


        with_components = (self.plot_comps.IsChecked() and len(dgroup.ycomps) > 1)

        self.plot_fitmodel(dgroup, show_resid=True, with_components=with_components)

        # print(" == fit model == ", self.fit_model)
        # print(" == fit result == ", result)

        result.model_repr = self.fit_model._reprstring(long=True)

        self.autosave_modelresult(result)
        if not hasattr(dgroup, 'fit_history'):
            dgroup.fit_history = []
        dgroup.fit_history.append(result)


        self.parent.show_subframe('result_frame', FitResultFrame,
                                  datagroup=dgroup,
                                  controller=self.controller)

        # self.update_start_values(result)
        self.savebtn.Enable()

        for m in self.parent.afterfit_menus:
            self.parent.menuitems[m].Enable(True)

    def update_start_values(self, result):
        """fill parameters with best fit values"""
        allparwids = {}
        for comp in self.fit_components.values():
            if comp.usebox is not None and comp.usebox.IsChecked():
                for name, parwids in comp.parwids.items():
                    allparwids[name] = parwids

        for pname, par in result.params.items():
            if pname in allparwids:
                allparwids[pname].value.SetValue(par.value)

    def autosave_modelresult(self, result, fname=None):
        """autosave model result to user larch folder"""
        xasguidir = os.path.join(site_config.usr_larchdir, 'xasgui')
        if not os.path.exists(xasguidir):
            try:
                os.makedirs(xasguidir)
            except OSError:
                print("Warning: cannot create XAS GUI user folder")
                return
        if not HAS_MODELSAVE:
            print("Warning: cannot save model results: upgrade lmfit")
            return
        if fname is None:
            fname = 'autosave.fitresult'
        fname = os.path.join(xasguidir, fname)
        save_modelresult(result, fname)

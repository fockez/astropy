# Licensed under a 3-clause BSD style license - see LICENSE.rst

"""
This module implements classes (called Fitters) which combine optimization
algorithms (typically from `scipy.optimize`) with statistic functions to perfom
fitting. Fitters are implemented as callable classes. In addition to the data
to fit, the ``__call__`` method takes an instance of
`~astropy.modeling.core.FittableModel` as input, and returns a copy of the
model with its parameters determined by the optimizer.

Optimization algorithms, called "optimizers" are implemented in
`~astropy.modeling.optimizers` and statistic functions are in
`~astropy.modeling.statistic`. The goal is to provide an easy to extend
framework and allow users to easily create new fitters by combining statistics
with optimizers.

There are two exceptions to the above scheme.
`~astropy.modeling.fitting.LinearLSQFitter` uses Numpy's `~numpy.linalg.lstsq`
function.  `~astropy.modeling.fitting.LevMarLSQFitter` uses
`~scipy.optimize.leastsq` which combines optimization and statistic in one
implementation.
"""

from __future__ import (absolute_import, unicode_literals, division,
                        print_function)

import abc
import warnings
import inspect
from functools import reduce

import numpy as np

from .utils import poly_map_domain
from ..utils.exceptions import AstropyUserWarning
from .core import _CompositeModel
from ..extern import six
from .optimizers import (SLSQP, Simplex)
from .statistic import (leastsquare)


__all__ = ['LinearLSQFitter', 'LevMarLSQFitter', 'SLSQPLSQFitter',
           'SimplexLSQFitter', 'JointFitter', 'Fitter']



# Statistic functions implemented in `astropy.modeling.statistic.py
STATISTICS = [leastsquare]

# Optimizers implemented in `astropy.modeling.optimizers.py
OPTIMIZERS = [Simplex, SLSQP]

from .optimizers import (DEFAULT_MAXITER, DEFAULT_EPS, DEFAULT_ACC)


class ModelsError(Exception):
    """Base class for model exceptions"""


class ModelLinearityError(ModelsError):
    """ Raised when a non-linear model is passed to a linear fitter."""


class UnsupportedConstraintError(ModelsError, ValueError):
    """
    Raised when a fitter does not support a type of constraint.
    """


@six.add_metaclass(abc.ABCMeta)
class Fitter(object):
    """
    Base class for all fitters.

    Parameters
    ----------
    optimizer : callable
        A callble implementing an optimization algorithm
    statistic : callable
        Statistic function
    """

    def __init__(self, optimizer, statistic):
        if optimizer is None:
            raise ValueError("Expected an optimizer.")
        if statistic is None:
            raise ValueError("Expected a statistic function.")
        if inspect.isclass(optimizer):
            # a callable class
            self._opt_method = optimizer()
        elif inspect.isfunction(optimizer):
            self._opt_method = optimizer
        else:
            raise ValueError("Expected optimizer to be a callable class or a function.")
        if inspect.isclass(statistic):
            self._stat_method = statistic()
        else:
            self._stat_method = statistic

    def objective_function(self, fps, *args):
        """
        Function to minimize

        Parameters
        ----------
        fps : list
            parameters returned by the fitter
        args : list
            [model, [other_args], [input coordinates]]
            other_args may include weights or any other quantities specific for a statistic

        Notes
        -----
        The list of arguments (args) is set in the `__call__` method.
        Fitters may overwrite this method, e.g. when statistic functions
        require other aguments.

        """
        model = args[0]
        meas = args[-1]
        _fitter_to_model_params(model, fps)
        res = self._stat_method(meas, model, *args[1:-1])
        return res

    @abc.abstractmethod
    def __call__(self):
        """
        This method performs the actual fitting and modifies the parameter list
        of a model.

        Fitter subclasses should implement this method.
        """

        raise NotImplementedError("Subclasses should implement this method.")


class LinearLSQFitter(object):
    """
    A class performing a linear least square fitting.

    Uses `numpy.linalg.lstsq` to do the fitting.
    Given a model and data, fits the model to the data and changes the
    model's parameters. Keeps a dictionary of auxiliary fitting information.
    """

    supported_constraints = ['fixed']

    def __init__(self):
        self.fit_info = {'residuals': None,
                         'rank': None,
                         'singular_values': None,
                         'params': None
                         }

    @staticmethod
    def _deriv_with_constraints(model, param_indices, x=None, y=None):
        if y is None:
            d = np.array(model.fit_deriv(x, *model.parameters))
        else:
            d = np.array(model.fit_deriv(x, y, *model.parameters))

        if model.col_fit_deriv:
            return d[param_indices]
        else:
            return d[:, param_indices]

    def _map_domain_window(self, model, x, y=None):
        """
        Maps domain into window for a polynomial model which has these
        attributes.
        """

        if y is None:
            if hasattr(model, 'domain') and model.domain is None:
                model.domain = [x.min(), x.max()]
            if hasattr(model, 'window') and model.window is None:
                model.window = [-1, 1]
            return poly_map_domain(x, model.domain, model.window)
        else:
            if hasattr(model, 'x_domain') and model.x_domain is None:
                model.x_domain = [x.min(), x.max()]
            if hasattr(model, 'y_domain') and model.y_domain is None:
                model.y_domain = [y.min(), y.max()]
            if hasattr(model, 'x_window') and model.x_window is None:
                model.x_window = [-1., 1.]
            if hasattr(model, 'y_window') and model.y_window is None:
                model.y_window = [-1., 1.]

            xnew = poly_map_domain(x, model.x_domain, model.x_window)
            ynew = poly_map_domain(y, model.y_domain, model.y_window)
            return xnew, ynew

    def __call__(self, model, x, y, z=None, weights=None, rcond=None):
        """
        Fit data to this model.

        Parameters
        ----------
        model : `~astropy.modeling.FittableModel`
            model to fit to x, y, z
        x : array
            input coordinates
        y : array
            input coordinates
        z : array (optional)
            input coordinates
        weights : array (optional)
            weights
        rcond :  float, optional
            Cut-off ratio for small singular values of ``a``.
            Singular values are set to zero if they are smaller than ``rcond``
            times the largest singular value of ``a``.

        Returns
        -------
        model_copy : `~astropy.modeling.FittableModel`
            a copy of the input model with parameters set by the fitter
        """
        if not model.fittable:
            raise ValueError("Model must be a subclass of FittableModel")
        if not model.linear:
            raise ModelLinearityError('Model is not linear in parameters, '
                                      'linear fit methods should not be used.')

        if any(model.tied.values()) \
                or any([tuple(b) != (None, None) for b in model.bounds.values()]) \
                or model.eqcons or model.ineqcons:
            raise ValueError("LinearFitter supports only fixed constraints.")
        multiple = False
        model_copy = model.copy()
        _, fitparam_indices = _model_to_fit_params(model_copy)
        if model_copy.n_inputs == 2 and z is None:
            raise ValueError("Expected x, y and z for a 2 dimensional model.")

        farg = _convert_input(x, y, z)

        if len(farg) == 2:
            x, y = farg
            if y.ndim == 2:
                if y.shape[1] != model_copy.param_dim:
                    raise ValueError("Number of data sets (Y array is expected"
                                     " to equal the number of parameter sets")
            # map domain into window
            if hasattr(model_copy, 'domain'):
                x = self._map_domain_window(model_copy, x)
            if any(model_copy.fixed.values()):
                lhs = self._deriv_with_constraints(model_copy,
                                                   fitparam_indices,
                                                   x=x)
            else:
                lhs = model_copy.fit_deriv(x, *model_copy.parameters)
            if len(y.shape) == 2:
                rhs = y
                multiple = y.shape[1]
            else:
                rhs = y
        else:
            x, y, z = farg
            if x.shape[-1] != z.shape[-1]:
                raise ValueError("x and z should have equal last dimensions")

            # map domain into window
            if hasattr(model_copy, 'x_domain'):
                x, y = self._map_domain_window(model_copy, x, y)

            if any(model_copy.fixed.values()):
                lhs = self._deriv_with_constraints(model_copy,
                                                   fitparam_indices, x=x, y=y)
            else:
                lhs = model_copy.fit_deriv(x, y, *model_copy.parameters)
            if len(z.shape) == 3:
                rhs = np.array([i.flatten() for i in z]).T
                multiple = z.shape[0]
            else:
                rhs = z.flatten()
        # If the derivative is defined along rows (as with non-linear models)
        if model_copy.col_fit_deriv:
            lhs = np.asarray(lhs).T
        if weights is not None:
            weights = np.asarray(weights, dtype=np.float)
            if len(x) != len(weights):
                raise ValueError("x and weights should have the same length")
            if rhs.ndim == 2:
                lhs *= weights[:, np.newaxis]
                rhs *= weights[:, np.newaxis]
            else:
                lhs *= weights[:, np.newaxis]
                rhs *= weights

        if not multiple and model_copy.param_dim > 1:
            raise ValueError("Attempting to fit a 1D data set to a model "
                             "with multiple parameter sets")
        if rcond is None:
            rcond = len(x) * np.finfo(x.dtype).eps

        scl = (lhs * lhs).sum(0)
        lacoef, resids, rank, sval = np.linalg.lstsq(lhs / scl, rhs, rcond)

        self.fit_info['residuals'] = resids
        self.fit_info['rank'] = rank
        self.fit_info['singular_values'] = sval

        # If y.n_inputs > model.n_inputs we are doing a simultanious 1D fitting
        # of several 1D arrays. Otherwise the model is 2D.
        # if y.n_inputs > self.model.n_inputs:
        if multiple and model_copy.param_dim != multiple:
            model_copy.param_dim = multiple
        # TODO: Changing the model's param_dim needs to be handled more
        # carefully; for now it's not actually allowed
        lacoef = (lacoef.T / scl).T
        self.fit_info['params'] = lacoef
        # TODO: Only Polynomial models currently have an _order attribute;
        # maybe change this to read isinstance(model, PolynomialBase)
        if hasattr(model_copy, '_order') and rank != model_copy._order:
            warnings.warn("The fit may be poorly conditioned\n",
                          AstropyUserWarning)
        _fitter_to_model_params(model_copy, lacoef.flatten())
        return model_copy


class LevMarLSQFitter(object):
    """
    Levenberg-Marquardt algorithm and least squares statistic.

    Attributes
    ----------
    fit_info : dict
        The `scipy.optimize.leastsq` result for the most recent fit (see
        notes).

    Notes
    -----
    The ``fit_info`` dictionary contains the values returned by
    `scipy.optimize.leastsq` for the most recent fit, including the values from
    the ``infodict`` dictionary it returns. See the `scipy.optimize.leastsq`
    documentation for details on the meaning of these values. Note that the
    ``x`` return value is *not* included (as it is instead the parameter values
    of the returned model).

    Additionally, one additional element of ``fit_info`` is computed whenever a
    model is fit, with the key 'param_cov'. The corresponding value is the
    covariance matrix of the parameters as a 2D numpy array.  The order of the
    matrix elements matches the order of the parameters in the fitted model
    (i.e., the same order as ``model.param_names``).
    """

    supported_constraints = ['fixed', 'tied', 'bounds']
    """
    The constaint types supported by this fitter type.
    """

    def __init__(self):
        self.fit_info = {'nfev': None,
                         'fvec': None,
                         'fjac': None,
                         'ipvt': None,
                         'qtf': None,
                         'message': None,
                         'ierr': None,
                         'param_jac': None,
                         'param_cov': None}

        super(LevMarLSQFitter, self).__init__()

    def objective_function(self, fps, *args):
        """
        Function to minimize

        Parameters
        ----------
        fps : list
            parameters returned by the fitter
        args : list
            [model, [weights], [input coordinates]]
        """

        model = args[0]
        weights = args[1]
        _fitter_to_model_params(model, fps)
        meas = args[-1]
        if weights is None:
            return np.ravel(model(*args[2 : -1]) - meas)
        else:
            return np.ravel(weights * (model(*args[1 : -1]) - meas))

    def __call__(self, model, x, y, z=None, weights=None,
                 maxiter=DEFAULT_MAXITER, acc=DEFAULT_ACC,
                 epsilon=DEFAULT_EPS, estimate_jacobian=False):
        """
        Fit data to this model.

        Parameters
        ----------
        model : `~astropy.modeling.FittableModel`
            model to fit to x, y, z
        x : array
           input coordinates
        y : array
           input coordinates
        z : array (optional)
           input coordinates
        weights : array (optional
           weights
        maxiter : int
            maximum number of iterations
        acc : float
            Relative error desired in the approximate solution
        epsilon : float
            A suitable step length for the forward-difference
            approximation of the Jacobian (if model.fjac=None). If
            epsfcn is less than the machine precision, it is
            assumed that the relative errors in the functions are
            of the order of the machine precision.
        estimate_jacobian : bool
            If False (default) and if the model has a fit_deriv method,
            it will be used. Otherwise the Jacobian will be estimated.
            If True, the Jacobian will be estimated in any case.

        Returns
        -------
        model_copy : `~astropy.modeling.FittableModel`
            a copy of the input model with parameters set by the fitter
        """

        from scipy import optimize

        model_copy = _validate_model(model, self.supported_constraints)
        farg = (model_copy, weights, ) + _convert_input(x, y, z)

        if model_copy.fit_deriv is None or estimate_jacobian:
            dfunc = None
        else:
            dfunc = self._wrap_deriv
        init_values, _ = _model_to_fit_params(model_copy)
        fitparams, cov_x, dinfo, mess, ierr = optimize.leastsq(
            self.objective_function, init_values, args=farg, Dfun=dfunc,
            col_deriv=model_copy.col_fit_deriv, maxfev=maxiter, epsfcn=epsilon,
            xtol=acc, full_output=True)
        _fitter_to_model_params(model_copy, fitparams)
        self.fit_info.update(dinfo)
        self.fit_info['cov_x'] = cov_x
        self.fit_info['message'] = mess
        self.fit_info['ierr'] = ierr
        if ierr not in [1, 2, 3, 4]:
            warnings.warn("The fit may be unsuccessful; check "
                          "fit_info['message'] for more information.",
                          AstropyUserWarning)

        # now try to compute the true covariance matrix
        if (len(y) > len(init_values)) and cov_x is not None:
            sum_sqrs = np.sum(self.objective_function(fitparams, *farg)**2)
            dof = len(y) - len(init_values)
            self.fit_info['param_cov'] = cov_x * sum_sqrs / dof
        else:
            self.fit_info['param_cov'] = None

        return model_copy

    @staticmethod
    def _wrap_deriv(params, model, weights, x, y, z=None):
        """
        Wraps the method calculating the Jacobian of the function to account
        for model constraints.

        `scipy.optimize.leastsq` expects the function derivative to have the
        above signature (parlist, (argtuple)). In order to accomodate model
        constraints, instead of using p directly, we set the parameter list in
        this function.
        """
        if any(model.fixed.values()) or any(model.tied.values()):

            if z is None:
                full_deriv = np.array(model.fit_deriv(x, *model.parameters))
            else:
                full_deriv = np.array(model.fit_deriv(x, y, *model.parameters))

            pars = [getattr(model, name) for name in model.param_names]
            fixed = [par.fixed for par in pars]
            tied = [par.tied for par in pars]
            tied = list(np.where([par.tied is not False for par in pars],
                                 True, tied))
            fix_and_tie = np.logical_or(fixed, tied)
            ind = np.logical_not(fix_and_tie)

            if not model.col_fit_deriv:
                full_deriv = np.asarray(full_deriv).T
                residues = np.asarray(full_deriv[np.nonzero(ind)])
            else:
                residues = full_deriv[np.nonzero(ind)]

            return [np.ravel(_) for _ in residues]
        else:
            if z is None:
                return model.fit_deriv(x, *params)
            else:
                return [np.ravel(_) for _ in model.fit_deriv(x, y, *params)]


class SLSQPLSQFitter(Fitter):
    """
    SLSQP optimization algorithm and least squares statistic.


    Raises
    ------
    ModelLinearityError
        A linear model is passed to a nonlinear fitter

    """

    supported_constraints = SLSQP.supported_constraints

    def __init__(self):
        super(SLSQPLSQFitter, self).__init__(optimizer=SLSQP, statistic=leastsquare)
        self.fit_info = {}

    def __call__(self, model, x, y, z=None, weights=None, **kwargs):
        """
        Fit data to this model.

        Parameters
        ----------
        model : `ParametricModel`
            model to fit to x, y, z
        x : array
            input coordinates
        y : array
            input coordinates
        z : array (optional)
            input coordinates
        weights : array (optional)
            weights
        kwargs : dict
            optional keyword arguments to be passed to the optimizer or the statistic

        verblevel : int
            0-silent
            1-print summary upon completion,
            2-print summary after each iteration
        maxiter : int
            maximum number of iterations
        epsilon : float
            the step size for finite-difference derivative estimates
        acc : float
            Requested accuracy

        Returns
        ------
        model_copy : `ParametricModel`
            a copy of the input model with parameters set by the fitter
        """

        model_copy = _validate_model(model, self._opt_method.supported_constraints)
        farg = _convert_input(x, y, z)
        farg = (model_copy, weights, ) + farg
        p0, _ = _model_to_fit_params(model_copy)
        fitparams, self.fit_info = self._opt_method(
            self.objective_function, p0, farg, **kwargs)
        _fitter_to_model_params(model_copy, fitparams)

        return model_copy


class SimplexLSQFitter(Fitter):
    """

    Simplex algorithm and least squares statistic.

    Raises
    ------
    ModelLinearityError
        A linear model is passed to a nonlinear fitter

    """

    supported_constraints = Simplex.supported_constraints

    def __init__(self):
        super(SimplexLSQFitter, self).__init__(optimizer=Simplex,
                                               statistic=leastsquare)
        self.fit_info = {}

    def __call__(self, model, x, y, z=None, weights=None, **kwargs):
        """
        Fit data to this model.

        Parameters
        ----------
        model : `~astropy.modeling.FittableModel`
            model to fit to x, y, z
        x : array
            input coordinates
        y : array
            input coordinates
        z : array (optional)
            input coordinates
        weights : array (optional)
            weights
        kwargs : dict
            optional keyword arguments to be passed to the optimizer or the statistic

        maxiter : int
            maximum number of iterations
        epsilon : float
            the step size for finite-difference derivative estimates
        acc : float
            Relative error in approximate solution

        Returns
        -------
        model_copy : `~astropy.modeling.FittableModel`
            a copy of the input model with parameters set by the fitter
        """

        model_copy = _validate_model(model,
                                     self._opt_method.supported_constraints)
        farg = _convert_input(x, y, z)
        farg = (model_copy, weights, ) + farg

        p0, _ = _model_to_fit_params(model_copy)

        fitparams, self.fit_info = self._opt_method(
            self.objective_function, p0, farg, **kwargs)
        _fitter_to_model_params(model_copy, fitparams)
        return model_copy


class JointFitter(object):
    """
    Fit models which share a parameter.

    For example, fit two gaussians to two data sets but keep
    the FWHM the same.

    Parameters
    ----------
    models : list
        a list of model instances
    jointparameters : list
        a list of joint parameters
    initvals : list
        a list of initial values
    """

    def __init__(self, models, jointparameters, initvals):
        self.models = list(models)
        self.initvals = list(initvals)
        self.jointparams = jointparameters
        self._verify_input()
        self.fitparams = self._model_to_fit_params()

        # a list of model.n_inputs
        self.modeldims = [m.n_inputs for m in self.models]
        # sum all model dimensions
        self.ndim = np.sum(self.modeldims)

    def _model_to_fit_params(self):
        fparams = []
        fparams.extend(self.initvals)
        for model in self.models:
            params = [p.flatten() for p in model.parameters]
            joint_params = self.jointparams[model]
            for param_name in joint_params:
                slc = model._param_metrics[param_name][0]
                del params[slc]
            fparams.extend(params)
        return fparams

    def objective_function(self, fps, *args):
        """
        fps : list
            the fitted parameters - result of an one iteration of the
            fitting algorithm
        args : dict
            tuple of measured and input coordinates
            args is always passed as a tuple from optimize.leastsq
        """

        lstsqargs = list(args)
        fitted = []
        fitparams = list(fps)
        numjp = len(self.initvals)
        # make a separate list of the joint fitted parameters
        jointfitparams = fitparams[:numjp]
        del fitparams[:numjp]

        for model in self.models:
            joint_params = self.jointparams[model]
            margs = lstsqargs[:model.n_inputs + 1]
            del lstsqargs[:model.n_inputs + 1]
            # separate each model separately fitted parameters
            numfp = len(model._parameters) - len(joint_params)
            mfparams = fitparams[:numfp]

            del fitparams[:numfp]
            # recreate the model parameters
            mparams = []
            for param_name in model.param_names:
                if param_name in joint_params:
                    index = joint_params.index(param_name)
                    # should do this with slices in case the
                    # parameter is not a number
                    mparams.extend([jointfitparams[index]])
                else:
                    slc = model._param_metrics[param_name][0]
                    plen = slc.stop - slc.start
                    mparams.extend(mfparams[:plen])
                    del mfparams[:plen]
            modelfit = model.eval(margs[:-1], *mparams)
            fitted.extend(modelfit - margs[-1])
        return np.ravel(fitted)

    def _verify_input(self):
        if len(self.models) <= 1:
            raise TypeError("Expected >1 models, %d is given" %
                            len(self.models))
        if len(self.jointparams.keys()) < 2:
            raise TypeError("At least two parameters are expected, "
                            "%d is given" % len(self.jointparams.keys()))
        for j in self.jointparams.keys():
            if len(self.jointparams[j]) != len(self.initvals):
                raise TypeError("%d parameter(s) provided but %d expected" %
                                (len(self.jointparams[j]), len(self.initvals)))

    def __call__(self, *args):
        """
        Fit data to these models keeping some of the pramaters common to the
        two models.
        """

        from scipy import optimize

        if len(args) != reduce(lambda x, y: x + 1 + y + 1, self.modeldims):
            raise ValueError("Expected %d coordinates in args but %d provided"
                             % (reduce(lambda x, y: x + 1 + y + 1,
                                       self.modeldims), len(args)))

        self.fitparams[:], _ = optimize.leastsq(self.objective_function,
                                                self.fitparams, args=args)

        fparams = self.fitparams[:]
        numjp = len(self.initvals)
        # make a separate list of the joint fitted parameters
        jointfitparams = fparams[:numjp]
        del fparams[:numjp]

        for model in self.models:
            # extract each model's fitted parameters
            joint_params = self.jointparams[model]
            numfp = len(model._parameters) - len(joint_params)
            mfparams = fparams[:numfp]

            del fparams[:numfp]
            # recreate the model parameters
            mparams = []
            for param_name in model.param_names:
                if param_name in joint_params:
                    index = joint_params.index(param_name)
                    # should do this with slices in case the parameter
                    # is not a number
                    mparams.extend([jointfitparams[index]])
                else:
                    slc = model._param_metrics[param_name][0]
                    plen = slc.stop - slc.start
                    mparams.extend(mfparams[:plen])
                    del mfparams[:plen]
            model.parameters = np.array(mparams)


def _convert_input(x, y, z=None):
    """Convert inputs to float arrays."""

    x = np.asarray(x, dtype=np.float)
    y = np.asarray(y, dtype=np.float)
    if x.shape[0] != y.shape[0]:
        raise ValueError("x and y should have the same shape")
    if z is None:
        farg = (x, y)
    else:
        z = np.asarray(z, dtype=np.float)
        if x.shape != z.shape:
            raise ValueError("x, y and z should have the same shape")
        farg = (x, y, z)
    return farg


# TODO: These utility functions are really particular to handling
# bounds/tied/fixed constraints for scipy.optimize optimizers that do not
# support them inherently; this needs to be reworked to be clear about this
# distinction (and the fact that these are not necessarily applicable to any
# arbitrary fitter--as evidenced for example by the fact that JointFitter has
# its own versions of these)
def _fitter_to_model_params(model, fps):
    """
    Constructs the full list of model parameters from the fitted and
    constrained parameters.
    """

    _fit_params, _fit_param_indices = _model_to_fit_params(model)
    if any(model.fixed.values()) or any(model.tied.values()):
        model.parameters[_fit_param_indices] = fps
        for idx, name in enumerate(model.param_names):
            if model.tied[name] != False:
                value = model.tied[name](model)
                slice_ = model._param_metrics[name][0]
                model.parameters[slice_] = value
    elif any([tuple(b) != (None, None) for b in model.bounds.values()]):
        for name, par in zip(model.param_names, _fit_params):
            if model.bounds[name] != (None, None):
                b = model.bounds[name]
                if b[0] is not None:
                    par = max(par, model.bounds[name][0])
                    if b[1] is not None:
                        par = min(par, model.bounds[name][1])
                    setattr(model, name, par)
    else:
        model.parameters = fps


def _model_to_fit_params(model):
    """
    Convert a model instance's parameter array to an array that can be used
    with a fitter that doesn't natively support fixed or tied parameters.
    In particular, it removes fixed/tied parameters from the parameter
    array.

    These may be a subset of the model parameters, if some of them are held
    constant or tied.
    """

    fitparam_indices = list(range(len(model.param_names)))
    if any(model.fixed.values()) or any(model.tied.values()):
        params = list(model.parameters)
        for idx, name in list(enumerate(model.param_names))[::-1]:
            if model.fixed[name] or model.tied[name]:
                sl = model._param_metrics[name][0]
                del params[sl]
                del fitparam_indices[idx]
        return (np.array(params), fitparam_indices)
    else:
        return (model.parameters, fitparam_indices)


def _validate_constraints(supported_constraints, model):
    """Make sure model constraints are supported by the current fitter."""

    message = 'Optimizer cannot handle {0} constraints.'

    if (any(model.fixed.values()) and
            'fixed' not in supported_constraints):
        raise UnsupportedConstraintError(
                message.format('fixed parameter'))

    if (any(model.tied.values()) and
            'tied' not in supported_constraints):
        raise UnsupportedConstraintError(
                message.format('tied parameter'))

    if (any([tuple(b) != (None, None) for b in model.bounds.values()]) and
            'bounds' not in supported_constraints):
        raise UnsupportedConstraintError(
                message.format('bound parameter'))

    if model.eqcons and 'eqcons' not in supported_constraints:
        raise UnsupportedConstraintError(message.format('equality'))

    if (model.ineqcons and
            'ineqcons' not in supported_constraints):
        raise UnsupportedConstraintError(message.format('inequality'))


def _validate_model(model, supported_constraints):
    """
    Check that model and fitter are compatible and return a copy of the model.
    """

    if not model.fittable:
        raise ValueError("Model does not appear to be fittable.")
    if model.linear:
        warnings.warn('Model is linear in parameters; '
                      'consider using linear fitting methods.',
                      AstropyUserWarning)
    if model.param_dim != 1:
        # for now only single data sets ca be fitted
        raise ValueError("Non-linear fitters can only fit "
                         "one data set at a time.")
    _validate_constraints(supported_constraints, model)

    model_copy = model.copy()
    return model_copy

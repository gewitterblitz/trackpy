import logging
import numpy as np
from scipy import stats
from scipy import interpolate
import pidly
import diagnostics
import plots

pi = np.pi

logger = logging.getLogger(__name__)

def idl_track(query, max_disp, min_appearances, memory=3):
    """Call Crocker/Weeks track.pro from IDL using pidly module.
    Returns one big array, where the last column is the probe ID."""
    idl = pidly.IDL()
    logger.info("Opened IDL process.")
    idl('pt = get_sql("{}")'.format(query))
    logger.info("IDL is done loading features from the database. Now tracking....")
    idl('t=track(pt, {}, goodenough={}, memory={})'.format(
        max_disp, min_appearances, memory))
    logger.info("IDL finished tracking. Now piping data into Python....")
    # 0: x, 1: y, 2: mass, 3: size, 4: ecc, 5: frame, 6: probe_id
    t = idl.ev('t')
    idl.close()
    return t

def split_by_probe(track_array):
    """Split an array of all trajectories, indexed by probe,
    into a list of arrays,
    where each array coresponds is a separate probe."""
    boundaries, = np.where(np.diff(track_array[:, 0], axis=0) > 0.0)
    boundaries += 1
    probes = np.split(track_array[:, 1:], boundaries)
    # 0: frame, 1: x, 2: y
    return probes

def interp(traj):
    """Linearly interpolate through gaps in the trajectory
    where the probe was not observed."""
    first_frame, last_frame = traj[:, 0][[0,-1]]
    full_domain = np.arange(first_frame, 1 + last_frame)
    interpolator = interpolate.interp1d(traj[:, 0], traj[:, 1:], axis=0)
    return np.column_stack((full_domain, interpolator(full_domain)))

def displacement(x, dt):
    """Return difference between neighbors separated by dt steps (frames).
    This is not the same as numpy.diff(x, n), the nth-order derivative."""
    return x[dt:]-x[:-dt]

def msd(traj, mpp, fps, max_interval=None, detail=False):
    """Compute the mean displacement and mean squared displacement of a
    trajectory over a range of time intervals. Input in units of px and frames;
    output in units of microns and seconds."""
    max_interval = max_interval if max_interval else 50 # default
    max_interval = min(max_interval, traj.shape[0])
    intervals = xrange(1, 1 + max_interval)
    traj = interp(traj)
    _msd = _detailed_msd if detail else _simple_msd
    results = [_msd(traj, i, mpp, fps) for i in intervals]
    return np.vstack(results)
     
def _detailed_msd(traj, interval, mpp, fps):
    """Given a continuous trajectory and a time interval (in frames), 
    return t, <x>, <y>, <r>, <x^2>, <y^2>, <r^2>, N."""
    d = displacement(mpp*traj[:, 1:], interval) # [[dx, dy], ...]
    sd = d**2
    stuff = np.column_stack((d, np.sum(d, axis=1), sd, np.sum(sd, axis=1)))
    # [[dx, dy, dr, dx^2, dy^2, dr^2], ...]
    mean_stuff = np.mean(stuff, axis=0)
    # Estimate statistically independent measurements:
    N = np.round(2*stuff.shape[0]/float(interval))
    return np.append(np.array([interval])/float(fps), mean_stuff, np.array([N])) 

def _simple_msd(traj, interval, mpp, fps):
    """Given a continuous trajectory and a time interval (in frames),
    return t, <r^2>."""
    d = displacement(mpp*traj[:, 1:], interval) # [[dx, dy], ...]
    sd = d**2
    msd_result = np.mean(np.sum(sd, axis=1), axis=0)
    return np.array([interval/float(fps), msd_result]) 

def ensemble_msd(probes, mpp, fps, max_interval=None):
    """Return ensemble mean squared displacement. Input in units of px
    and frames. Output in units of microns and seconds."""
    logger.info("%.3f microns per pixel, %d fps", mpp, fps)
    m = np.vstack([msd(traj, mpp, fps, max_interval, detail=False) \
                for traj in probes])
    m = m[m[:, 0].argsort()] # sort by dt 
    boundaries, = np.where(np.diff(m[:, 0], axis=0) > 0.0)
    boundaries += 1
    m = np.split(m, boundaries) # list of arrays, one for each dt
    ensm_m = np.vstack([np.mean(this_m, axis=0) for this_m in m])
    power, coeff = fit_powerlaw(ensm_m)
    return ensm_m

def fit_powerlaw(a):
    """Fit a power law to MSD data. Return the power and the coefficient.
    This is not a generic power law. By treating it as a linear regression in
    log space, we assume no additive constant: y = 0 + coeff*x**power."""
    slope, intercept, r, p, stderr = \
        stats.linregress(np.log(a[:, 0]), np.log(a[:, 1]))
    return slope, np.exp(intercept)

def drift(probes, suppress_plot=False):
    "Return the ensemble drift, x(t)."
    dx_list = [np.column_stack(
               (np.diff(x[:, 0]), x[1:, 0], np.diff(x[:, 1:], axis=0))
               ) for x in probes] # dt, t, dx, dy
    dx = np.vstack(dx_list) # dt, t, dx, dy
    dx = dx[dx[:, 0] == 1.0, 1:] # Drop entries where dt > 1 ( gap).
    dx = dx[dx[:, 0].argsort()] # sort by t
    boundaries, = np.where(np.diff(dx[:, 0], axis=0) > 0.0)
    boundaries += 1
    dx_list = np.split(dx, boundaries) # list of arrays, one for each t
    ensemble_dx = np.vstack([np.mean(dx, axis=0) for dx in dx_list])
    ensemble_dx = interp(ensemble_dx)
    uncertainty = np.vstack([np.concatenate(
                             (np.array([dx[0, 0]]), np.std(dx[:, 1:], axis=0))) 
                             for dx in dx_list])
    uncertainty = interp(uncertainty)
    # ensemble_dx is t, dx, dy. Integrate to get t, x, y.
    x = np.column_stack((ensemble_dx[:, 0], 
                         np.cumsum(ensemble_dx[:, 1:], axis=0)))
    if not suppress_plot: plots.plot_drift(x, uncertainty)
    return x, uncertainty

def cart_to_polar(x, y, deg=False):
    "Convert Cartesian x, y to r, theta in radians."
    conversion = 180./pi if deg else 1.
    return np.sqrt(x**2 + y**2), conversion*np.arctan2(y, x)

def subtract_drift(probes, d=None):
    "Return a copy of the track_array with the overall drift subtracted out."
    if d is None: 
        d, uncertainty = drift(probes)
    new_probes = list(probes) # copy list
    for p in new_probes:
        for t, x, y in d:
            p[p[:, 0] == t, 1:3] -= [x, y] 
    return new_probes

def is_localized(traj, threshold=0.4):
    "Is this probe's motion localized?"
    m = msd(traj, mpp=1., fps=1.)
    power, coeff = fit_powerlaw(m)
    return True if power < threshold else False

def is_diffusive(traj, threshold=0.85):
    "Is this probe's motion diffusive?"
    m = msd(traj, mpp=1., fps=1.)
    power, coeff = fit_powerlaw(m)
    return True if power > threshold else False

def is_unphysical(traj, mpp, fps, threshold=0.08):
    """Is the first MSD datapoint unphysically high? (This is sometimes an
    artifact of uneven drift.)"""
    m = msd(traj, mpp, fps=1.)
    return True if m[0, 1] > threshold else False

def split_branches(probes, threshold=0.85, lower_threshold=0.4):
    """Sort list of probes into three lists, sorted by mobility.
    Return: diffusive, localized, subdiffusive."""
    diffusive = [p for p in probes if is_diffusive(p)]
    localized = [p for p in probes if is_localized(p)]
    subdiffusive = [p for p in probes if ((not is_localized(p)) and \
                           (not is_diffusive(p)))]
    logger.info("{} diffusive, {} localized, {} subdiffusive",
             len(diffusive), len(localized), len(subdiffusive))
    return diffusive, localized, subdiffusive

def cast_probes(flexible_input, output_style='probes'):
    """Accept either the IDL-style track_array or a list of probes,
    and return one or the other."""
    if output_style == 'track array':
        if type(flexible_input) is np.ndarray:
            return flexible_input
        elif type(flexible_input) is list:
            return np.vstack(flexible_input)
        else:
            raise TypeError, ("Input must be either the np.ndarray track_array "
                              "or the list of probes.")
    elif output_style == 'probes':
        if type(flexible_input) is list:
            return flexible_input
        elif type(flexible_input) is np.ndarray:
            return split_by_probe(flexible_input)
        else:
            raise TypeError, ("Input must be either the np.ndarray track_array "
                              "or the list of probes.")
    else:
        raise ValueError, "output_style must be 'track array' or 'probes'."

import warnings

import numpy as np
import scipy.sparse as sp
from scipy.special import iv  # modified Bessel function of first kind, I_v
from numpy import i0  # modified Bessel function of first kind order 0, I_0
from scipy.special import logsumexp

from sklearn.base import BaseEstimator, ClusterMixin, TransformerMixin
from sklearn.cluster._kmeans import (
    _check_sample_weight,
    # _labels_inertia,
    _tolerance,
)
from sklearn.utils.validation import FLOAT_DTYPES
from sklearn.utils.validation import check_is_fitted
from sklearn.utils import check_array, check_random_state, as_float_array
from sklearn.preprocessing import normalize
from sklearn.utils.extmath import squared_norm
from sklearn.metrics.pairwise import cosine_distances
from joblib import Parallel, delayed

from . import spherical_kmeans


MAX_CONTENTRATION = 1e10


def _inertia_from_labels(X, centers, labels, v_weights = None):
    """Compute inertia with cosine distance using known labels.
    """
    if v_weights is None:
        v_weights = np.ones((X.shape[0],))

    n_examples, n_features = X.shape
    v_weights = v_weights / np.sum(v_weights) * n_examples
    inertia = np.zeros((n_examples,))
    for ee in range(n_examples):
        inertia[ee] = 1 - np.abs(X[ee, :].dot(centers[int(labels[ee]), :]).T)

    return np.sum(inertia*v_weights)


def _labels_inertia(X, centers, v_weights = None):
    """Compute labels and inertia with cosine distance.
    """
    n_examples, n_features = X.shape
    n_clusters, n_features = centers.shape

    if v_weights is None:
        v_weights = np.ones((n_examples,))
    v_weights = v_weights / np.sum(v_weights) * n_examples
    
    labels = np.zeros((n_examples,))
    inertia = np.zeros((n_examples,))

    for ee in range(n_examples):
        dists = np.zeros((n_clusters,))
        for cc in range(n_clusters):
            dists[cc] = 1 - np.abs(X[ee, :].dot(centers[cc, :]).T)

        labels[ee] = np.argmin(dists)
        inertia[ee] = dists[int(labels[ee])]

    return labels, np.sum(inertia*v_weights)


def _vmf_log(X, kappa, mu):
    """Computs log(vMF(X, kappa, mu)) using built-in numpy/scipy Bessel
    approximations.

    Works well on small kappa and mu.
    """
    n_examples, n_features = X.shape
    return np.log(_vmf_normalize(kappa, n_features) * np.exp(kappa * np.abs(X.dot(mu)).T))


def _vmf_normalize(kappa, dim):
    """Compute normalization constant using built-in numpy/scipy Bessel
    approximations.

    Works well on small kappa and mu.
    """
    num = np.power(kappa, dim / 2. - 1.)

    if dim / 2. - 1. < 1e-15:
        denom = np.power(2. * np.pi, dim / 2.) * i0(kappa)
    else:
        denom = np.power(2. * np.pi, dim / 2.) * iv(dim / 2. - 1., kappa)

    if np.isinf(num):
        raise ValueError("VMF scaling numerator was inf.")

    if np.isinf(denom):
        raise ValueError("VMF scaling denominator was inf.")

    if np.abs(denom) < 1e-15:
        raise ValueError("VMF scaling denominator was 0.")

    return num / denom


def _log_H_asymptotic(nu, kappa):
    """Compute the Amos-type upper bound asymptotic approximation on H where
    log(H_\nu)(\kappa) = \int_0^\kappa R_\nu(t) dt.

    See "lH_asymptotic <-" in movMF.R and utility function implementation notes
    from https://cran.r-project.org/web/packages/movMF/index.html
    """
    beta = np.sqrt((nu + 0.5) ** 2)
    kappa_l = np.min([kappa, np.sqrt((3. * nu + 11. / 2.) * (nu + 3. / 2.))])
    return _S(kappa, nu + 0.5, beta) + (
        _S(kappa_l, nu, nu + 2.) - _S(kappa_l, nu + 0.5, beta)
    )


def _S(kappa, alpha, beta):
    """Compute the antiderivative of the Amos-type bound G on the modified
    Bessel function ratio.

    Note:  Handles scalar kappa, alpha, and beta only.

    See "S <-" in movMF.R and utility function implementation notes from
    https://cran.r-project.org/web/packages/movMF/index.html
    """
    kappa = 1. * np.abs(kappa)
    alpha = 1. * alpha
    beta = 1. * np.abs(beta)
    a_plus_b = alpha + beta
    u = np.sqrt(kappa ** 2 + beta ** 2)
    if alpha == 0:
        alpha_scale = 0
    else:
        alpha_scale = alpha * np.log((alpha + u) / a_plus_b)

    return u - beta - alpha_scale


def _vmf_log_asymptotic(X, kappa, mu):
    """Compute log(f(x|theta)) via Amos approximation

        log(f(x|theta)) = theta' x - log(H_{d/2-1})(\|theta\|)

    where theta = kappa * X, \|theta\| = kappa.

    Computing _vmf_log helps with numerical stability / loss of precision for
    for large values of kappa and n_features.

    See utility function implementation notes in movMF.R from
    https://cran.r-project.org/web/packages/movMF/index.html
    """
    n_examples, n_features = X.shape
    log_vfm = kappa * np.abs(X.dot(mu)).T + -_log_H_asymptotic(n_features / 2. - 1., kappa)

    return log_vfm


def _log_likelihood(X, centers, weights, concentrations):
    if len(np.shape(X)) != 2:
        X = X.reshape((1, len(X)))

    n_examples, n_features = np.shape(X)
    n_clusters, _ = centers.shape

    if n_features <= 50:  # works up to about 50 before numrically unstable
        vmf_f = _vmf_log
    else:
        vmf_f = _vmf_log_asymptotic

    f_log = np.zeros((n_clusters, n_examples))
    for cc in range(n_clusters):
        f_log[cc, :] = vmf_f(X, concentrations[cc], centers[cc, :])

    posterior = np.zeros((n_clusters, n_examples))
    weights_log = np.log(weights)
    posterior = np.tile(weights_log.T, (n_examples, 1)).T + f_log
    for ee in range(n_examples):
        posterior[:, ee] = np.exp(posterior[:, ee] - logsumexp(posterior[:, ee]))

    return posterior

def _log_likelihood_total(X, centers, weights, concentrations):
    if len(np.shape(X)) != 2:
        X = X.reshape((1, len(X)))

    n_examples, n_features = np.shape(X)
    n_clusters, _ = centers.shape

    if n_features <= 50:  # works up to about 50 before numerically unstable
        vmf_f = _vmf_log
    else:
        vmf_f = _vmf_log_asymptotic

    f_log = np.zeros((n_clusters, n_examples))
    for cc in range(n_clusters):
        f_log[cc, :] = vmf_f(X, concentrations[cc], centers[cc, :])

    weights_log = np.log(weights)
    log_likelihoods = np.zeros(n_examples)
    
    for ee in range(n_examples):
        log_likelihoods[ee] = logsumexp(weights_log + f_log[:, ee])
    
    # Sum the log likelihoods of all data points to get the total log likelihood
    total_log_likelihood = np.sum(log_likelihoods)
    return total_log_likelihood


def _init_unit_centers(X, n_clusters, random_state, init, add_uniform_cluster=False):
    """Initializes unit norm centers.

    Parameters
    ----------
    X : array-like or sparse matrix, shape=(n_samples, n_features)

    n_clusters : int, optional, default: 8
        The number of clusters to form as well as the number of
        centroids to generate.

    random_state : integer or numpy.RandomState, optional
        The generator used to initialize the centers. If an integer is
        given, it fixes the seed. Defaults to the global numpy random
        number generator.

    init:  (string) one of
        k-means++ : uses sklearn k-means++ initialization algorithm
        spherical-k-means : use centroids from one pass of spherical k-means
        random : random unit norm vectors
        random-orthonormal : random orthonormal vectors
        If an ndarray is passed, it should be of shape (n_clusters, n_features)
        and gives the initial centers.
    """
    n_examples, n_features = np.shape(X)
    n_clusters = n_clusters + (1 if add_uniform_cluster else 0)
    if isinstance(init, np.ndarray):
        n_init_clusters, n_init_features = init.shape
        assert n_init_clusters == n_clusters
        assert n_init_features == n_features
        
        # ensure unit normed centers
        centers = init
        for cc in range(n_clusters):
            centers[cc, :] = centers[cc, :] / np.linalg.norm(centers[cc, :])

        return centers

    elif init == "spherical-k-means":
        labels, inertia, centers, iters = spherical_kmeans._spherical_kmeans_single_lloyd(
            X, n_clusters, x_squared_norms=np.ones((n_examples,)), init="k-means++"
        )

        return centers

    elif init == "random":
        centers = np.random.randn(n_clusters, n_features)
        for cc in range(n_clusters):
            centers[cc, :] = centers[cc, :] / np.linalg.norm(centers[cc, :])

        return centers

    elif init == "k-means++":
        centers = _init_centroids(
            X,
            n_clusters,
            "k-means++",
            random_state=random_state,
            x_squared_norms=np.ones((n_examples,)),
        )

        for cc in range(n_clusters):
            centers[cc, :] = centers[cc, :] / np.linalg.norm(centers[cc, :])

        return centers

    elif init == "random-orthonormal":
        centers = np.random.randn(n_clusters, n_features)
        q, r = np.linalg.qr(centers.T, mode="reduced")

        return q.T

    elif init == "random-class":
        centers = np.zeros((n_clusters, n_features))
        for cc in range(n_clusters):
            while np.linalg.norm(centers[cc, :]) == 0:
                labels = np.random.randint(0, n_clusters, n_examples)
                centers[cc, :] = X[labels == cc, :].sum(axis=0)

        for cc in range(n_clusters):
            centers[cc, :] = centers[cc, :] / np.linalg.norm(centers[cc, :])
    # if add_uniform_cluster:
    #     # Add an extra cluster with specific initialization
    #     # Here, we initialize it as a random unit vector
    #     extra_center = np.random.randn(n_features)
    #     extra_center /= np.linalg.norm(extra_center)

    #     if isinstance(centers, np.ndarray):
    #         centers = np.vstack([centers, extra_center])
    #     else:
    #         centers = extra_center.reshape(1, -1)
        return centers


def _expectation(X, centers, weights, concentrations, posterior_type="soft"):
    """Compute the log-likelihood of each datapoint being in each cluster.

    Parameters
    ----------
    centers (mu) : array, [n_centers x n_features]
    weights (alpha) : array, [n_centers, ] (alpha)
    concentrations (kappa) : array, [n_centers, ]

    Returns
    ----------
    posterior : array, [n_centers, n_examples]
    """
    n_examples, n_features = np.shape(X)
    n_clusters, _ = centers.shape

    if n_features <= 50:  # works up to about 50 before numrically unstable
        vmf_f = _vmf_log
    else:
        vmf_f = _vmf_log_asymptotic

    f_log = np.zeros((n_clusters, n_examples))
    for cc in range(n_clusters):
        f_log[cc, :] = vmf_f(X, concentrations[cc], centers[cc, :])

    posterior = np.zeros((n_clusters, n_examples))
    if posterior_type == "soft":
        weights_log = np.log(weights)
        posterior = np.tile(weights_log.T, (n_examples, 1)).T + f_log
        for ee in range(n_examples):
            posterior[:, ee] = np.exp(posterior[:, ee] - logsumexp(posterior[:, ee]))

    elif posterior_type == "hard":
        weights_log = np.log(weights)
        weighted_f_log = np.tile(weights_log.T, (n_examples, 1)).T + f_log
        for ee in range(n_examples):
            posterior[np.argmax(weighted_f_log[:, ee]), ee] = 1.0

    return posterior


# def _maximization(X, posterior, force_weights=None):
#     """Estimate new centers, weights, and concentrations from

#     Parameters
#     ----------
#     posterior : array, [n_centers, n_examples]
#         The posterior matrix from the expectation step.

#     force_weights : None or array, [n_centers, ]
#         If None is passed, will estimate weights.
#         If an array is passed, will use instead of estimating.

#     Returns
#     ----------
#     centers (mu) : array, [n_centers x n_features]
#     weights (alpha) : array, [n_centers, ] (alpha)
#     concentrations (kappa) : array, [n_centers, ]
#     """
#     n_examples, n_features = X.shape
#     n_clusters, n_examples = posterior.shape
#     concentrations = np.zeros((n_clusters,))
#     centers = np.zeros((n_clusters, n_features))
#     if force_weights is None:
#         weights = np.zeros((n_clusters,))

#     for cc in range(n_clusters):
#         # update weights (alpha)
#         if force_weights is None:
#             weights[cc] = np.mean(posterior[cc, :])
#         else:
#             weights = force_weights

#         # update centers (mu)
#         X_scaled = X.copy()
#         if sp.issparse(X):
#             X_scaled.data *= posterior[cc, :].repeat(np.diff(X_scaled.indptr))
#         else:
#             for ee in range(n_examples):
#                 X_scaled[ee, :] *= posterior[cc, ee]

#         centers[cc, :] = X_scaled.sum(axis=0)

#         # normalize centers
#         center_norm = np.linalg.norm(centers[cc, :])
#         if center_norm > 1e-8:
#             centers[cc, :] = centers[cc, :] / center_norm

#         # update concentration (kappa) [TODO: add other kappa approximations]
#         rbar = center_norm / (n_examples * weights[cc])
#         concentrations[cc] = rbar * n_features - np.power(rbar, 3.)
#         if np.abs(rbar - 1.0) < 1e-10:
#             concentrations[cc] = MAX_CONTENTRATION
#         else:
#             concentrations[cc] /= 1. - np.power(rbar, 2.)

#         # let python know we can free this (good for large dense X)
#         del X_scaled

#     return centers, weights, concentrations

def _maximization(X, posterior, v_weights=None, force_weights=None):
    """Estimate new centers, weights, and concentrations from

    Parameters
    ----------
    posterior : array, [n_centers, n_examples]
        The posterior matrix from the expectation step.

    force_weights : None or array, [n_centers, ]
        If None is passed, will estimate weights.
        If an array is passed, will use instead of estimating.

    Returns
    ----------
    centers (mu) : array, [n_centers x n_features]
    weights (alpha) : array, [n_centers, ] (alpha)
    concentrations (kappa) : array, [n_centers, ]
    """
    n_examples, n_features = X.shape
    n_clusters, n_examples = posterior.shape
    concentrations = np.zeros((n_clusters,))
    std_devs = np.zeros((n_clusters,))
    centers = np.zeros((n_clusters, n_features))
    if force_weights is None:
        weights = np.zeros((n_clusters,))

    if v_weights is None:
        v_weights = np.ones(n_examples)
        
    for cc in range(n_clusters):
        # update weights (alpha)
        if force_weights is None:
            weights[cc] = np.mean(posterior[cc, :]*v_weights)
        else:
            weights = force_weights

        # update centers (mu) and concentration (kappa) using fisher_stats
        weights_cluster = posterior[cc, :]
        centers[cc, :], concentrations[cc], std_devs[cc] = fisher_stats(X.T, weights=weights_cluster*v_weights)

    return centers, weights, concentrations, std_devs

def fisher_stats(xyz, weights=None, conf=68.27):
    """
    Returns the resultant vector from a series of longitudes and latitudes. If
    a confidence is set the function additionally returns the opening angle
    of the confidence small circle (Fisher, 19..) and the dispersion factor
    (kappa).

    Parameters
    ----------
    lons : array-like
        A sequence of longitudes (in radians)
    lats : array-like
        A sequence of latitudes (in radians)
    conf : confidence value
        The confidence used for the calculation (float). Defaults to None.

    Returns
    -------
    mean vector: tuple
        The point that lies in the center of a set of vectors.
        (Longitude, Latitude) in radians.

    If 1 vector is passed to the function it returns two None-values. For
    more than one vector the following 3 values are returned as a tuple:

    r_value: float
        The magnitude of the resultant vector (between 0 and 1) This represents
        the degree of clustering in the data.
    angle: float
        The opening angle of the small circle that corresponds to confidence
        of the calculated direction.
    kappa: float
        A measure for the amount of dispersion of a group of layers. For
        one vector the factor is undefined. Approaches infinity for nearly
        parallel vectors and zero for highly dispersed vectors.

    """

    if weights is None:
        weights = np.ones(len(xyz.T))
    weights = weights.copy()
    weights[np.isnan(weights)] = 1

    # Calculate the weighted orientation tensor
    orientation_tensor = np.zeros((3, 3))
    total_weight = 0
    for n, w in zip(xyz.T, weights):
        orientation_tensor += w * np.outer(n, n)
        total_weight += w

    orientation_tensor /= total_weight

    # Find eigenvectors and eigenvalues
    eigenvalues, eigenvectors = np.linalg.eig(orientation_tensor)

    # Select the eigenvector corresponding to the largest eigenvalue
    max_index = np.argmax(eigenvalues)
    mean_vector = eigenvectors[:, max_index]

    if len(xyz.T) > 1:
        # Calculate the weighted Fisher k value
        R = eigenvalues[max_index]
        k = (total_weight-1) / (total_weight - np.sqrt(R)*total_weight)

        # Calculate the variability angle associated with the specified confidence level
        p = (100-conf)/100
        fract1 = (1 - np.sqrt(R)) / (np.sqrt(R))
        fract3 = 1.0 / (total_weight - 1.0)
        confidence_angle = np.arccos(1 - fract1 * ((1 / p) ** fract3 - 1))
        confidence_angle = np.degrees(confidence_angle)
        variability_angle = confidence_angle*(total_weight-1)**0.5
        return mean_vector, k, variability_angle

    else:
        return None, None

def _movMF(
    X,
    n_clusters,
    v_weights=None,
    posterior_type="soft",
    force_weights=None,
    max_iter=300,
    verbose=False,
    init="random-class",
    random_state=None,
    tol=1e-6,
    add_uniform_cluster=False,
    random_weight_mod = 1.0
):
    """Mixture of von Mises Fisher clustering.

    Implements the algorithms (i) and (ii) from

      "Clustering on the Unit Hypersphere using von Mises-Fisher Distributions"
      by Banerjee, Dhillon, Ghosh, and Sra.

    TODO: Currently only supports Banerjee et al 2005 approximation of kappa,
          however, there are numerous other approximations see _update_params.

    Attribution
    ----------
    Approximation of log-vmf distribution function from movMF R-package.

    movMF: An R Package for Fitting Mixtures of von Mises-Fisher Distributions
    by Kurt Hornik, Bettina Grun, 2014

    Find more at:
      https://cran.r-project.org/web/packages/movMF/vignettes/movMF.pdf
      https://cran.r-project.org/web/packages/movMF/index.html

    Parameters
    ----------
    n_clusters : int, optional, default: 8
        The number of clusters to form as well as the number of
        centroids to generate.

    posterior_type: 'soft' or 'hard'
        Type of posterior computed in exepectation step.
        See note about attribute: self.posterior_

    force_weights : None or array [n_clusters, ]
        If None, the algorithm will estimate the weights.
        If an array of weights, algorithm will estimate concentrations and
        centers with given weights.

    max_iter : int, default: 300
        Maximum number of iterations of the k-means algorithm for a
        single run.

    n_init : int, default: 10
        Number of time the k-means algorithm will be run with different
        centroid seeds. The final results will be the best output of
        n_init consecutive runs in terms of inertia.

    init:  (string) one of
        random-class [default]: random class assignment & centroid computation
        k-means++ : uses sklearn k-means++ initialization algorithm
        spherical-k-means : use centroids from one pass of spherical k-means
        random : random unit norm vectors
        random-orthonormal : random orthonormal vectors
        If an ndarray is passed, it should be of shape (n_clusters, n_features)
        and gives the initial centers.

    tol : float, default: 1e-6
        Relative tolerance with regards to inertia to declare convergence

    n_jobs : int
        The number of jobs to use for the computation. This works by computing
        each of the n_init runs in parallel.
        If -1 all CPUs are used. If 1 is given, no parallel computing code is
        used at all, which is useful for debugging. For n_jobs below -1,
        (n_cpus + 1 + n_jobs) are used. Thus for n_jobs = -2, all CPUs but one
        are used.

    random_state : integer or numpy.RandomState, optional
        The generator used to initialize the centers. If an integer is
        given, it fixes the seed. Defaults to the global numpy random
        number generator.

    verbose : int, default 0
        Verbosity mode.

    copy_x : boolean, default True
        When pre-computing distances it is more numerically accurate to center
        the data first.  If copy_x is True, then the original data is not
        modified.  If False, the original data is modified, and put back before
        the function returns, but small numerical differences may be introduced
        by subtracting and then adding the data mean.
    """
    random_state = check_random_state(random_state)
    n_examples, n_features = np.shape(X)

    # init centers (mus)
    centers = _init_unit_centers(X, n_clusters, random_state, init, add_uniform_cluster=add_uniform_cluster)
    if add_uniform_cluster:
        n_clusters = n_clusters + 1
    # init weights (alphas)
    if force_weights is None:
        weights = np.ones((n_clusters,))
        weights = weights / np.sum(weights)
    else:
        weights = force_weights

    # init concentrations (kappas)
    concentrations = np.ones((n_clusters,))

    if add_uniform_cluster:
        concentrations[-1] = 0.1
        weights[-1] = np.max([weights[-1], np.sum(weights[0:-1])*random_weight_mod])
        weights = weights / np.sum(weights)

    if verbose:
        print("Initialization complete")

    for iter in range(max_iter):
        centers_prev = centers.copy()

        # expectation step
        posterior = _expectation(
            X, centers, weights, concentrations, posterior_type=posterior_type
        )

        # maximization step
        centers, weights, concentrations_updated, std_devs = _maximization(
            X, posterior, v_weights=v_weights, force_weights=force_weights
        )
        if add_uniform_cluster:
            concentrations[:-1] = concentrations_updated[:-1]
            average_of_weights = np.mean(weights[:-1])
            weights[-1] = np.max([weights[-1], np.sum(weights[0:-1])*random_weight_mod])
            weights = weights / np.sum(weights)
        else:
            concentrations = concentrations_updated
        # check convergence
        tolcheck = squared_norm(centers_prev - centers)
        if tolcheck <= tol:
            if verbose:
                print(
                    "Converged at iteration %d: "
                    "center shift %e within tolerance %e" % (iter, tolcheck, tol)
                )
            break

    # labels come for free via posterior
    labels = np.zeros((n_examples,))
    for ee in range(n_examples):
        labels[ee] = np.argmax(posterior[:, ee])

    inertia = _inertia_from_labels(X, centers, labels, v_weights=v_weights)

    return centers, weights, concentrations, std_devs, posterior, labels, inertia


def movMF(
    X,
    n_clusters,
    posterior_type="soft",
    force_weights=None,
    n_init=10,
    n_jobs=1,
    max_iter=300,
    verbose=False,
    init="random-class",
    random_state=None,
    tol=1e-6,
    copy_x=True,
    v_weights=None,
    add_uniform_cluster=False,
    random_weight_mod = 1.0,
):
    """Wrapper for parallelization of _movMF and running n_init times.
    """
    if n_init <= 0:
        raise ValueError(
            "Invalid number of initializations."
            " n_init=%d must be bigger than zero." % n_init
        )
    random_state = check_random_state(random_state)

    if max_iter <= 0:
        raise ValueError(
            "Number of iterations should be a positive number,"
            " got %d instead" % max_iter
        )

    best_inertia = np.infty
    X = as_float_array(X, copy=copy_x)
    tol = _tolerance(X, tol)

    if hasattr(init, "__array__"):
        init = check_array(init, dtype=X.dtype.type, copy=True)
        # _validate_center_shape(X, n_clusters, init)

        if n_init != 1:
            warnings.warn(
                "Explicit initial center position passed: "
                "performing only one init in k-means instead of n_init=%d" % n_init,
                RuntimeWarning,
                stacklevel=2,
            )
            n_init = 1

    # defaults
    best_centers = None
    best_labels = None
    best_weights = None
    best_concentrations = None
    best_std_devs = None
    best_posterior = None
    best_inertia = None

    if n_jobs == 1:
        # For a single thread, less memory is needed if we just store one set
        # of the best results (as opposed to one set per run per thread).
        for it in range(n_init):
            # cluster on the sphere
            (centers, weights, concentrations, std_devs, posterior, labels, inertia) = _movMF(
                X,
                n_clusters,
                v_weights=v_weights,
                posterior_type=posterior_type,
                force_weights=force_weights,
                max_iter=max_iter,
                verbose=verbose,
                init=init,
                random_state=random_state,
                tol=tol,
                add_uniform_cluster=add_uniform_cluster,
                random_weight_mod = random_weight_mod,
            )

            # determine if these results are the best so far
            if best_inertia is None or inertia < best_inertia:
                best_centers = centers.copy()
                best_labels = labels.copy()
                best_weights = weights.copy()
                best_concentrations = concentrations.copy()
                best_std_devs = std_devs.copy()
                best_posterior = posterior.copy()
                best_inertia = inertia
    else:
        # parallelisation of movMF runs
        seeds = random_state.randint(np.iinfo(np.int32).max, size=n_init)
        results = Parallel(n_jobs=n_jobs, verbose=0)(
            delayed(_movMF)(
                X,
                n_clusters,
                v_weights=v_weights,
                posterior_type=posterior_type,
                force_weights=force_weights,
                max_iter=max_iter,
                verbose=verbose,
                init=init,
                random_state=random_state,
                tol=tol,
                add_uniform_cluster=add_uniform_cluster,
                random_weight_mod = random_weight_mod,
            )
            for seed in seeds
        )

        # Get results with the lowest inertia
        centers, weights, concentrations, std_devs, posteriors, labels, inertia = zip(*results)
        best = np.argmin(inertia)
        best_labels = labels[best]
        best_inertia = inertia[best]
        best_centers = centers[best]
        best_concentrations = concentrations[best]
        best_std_devs = std_devs[best]
        best_posterior = posteriors[best]
        best_weights = weights[best]

    return (
        best_centers,
        best_labels,
        best_inertia,
        best_weights,
        best_concentrations,
        best_std_devs,
        best_posterior,
    )


class VonMisesFisherMixture(BaseEstimator, ClusterMixin, TransformerMixin):
    """Estimator for Mixture of von Mises Fisher clustering on the unit sphere.

    Implements the algorithms (i) and (ii) from

      "Clustering on the Unit Hypersphere using von Mises-Fisher Distributions"
      by Banerjee, Dhillon, Ghosh, and Sra.

    TODO: Currently only supports Banerjee et al 2005 approximation of kappa,
          however, there are numerous other approximations see _update_params.

    Attribution
    ----------
    Approximation of log-vmf distribution function from movMF R-package.

    movMF: An R Package for Fitting Mixtures of von Mises-Fisher Distributions
    by Kurt Hornik, Bettina Grun, 2014

    Find more at:
      https://cran.r-project.org/web/packages/movMF/vignettes/movMF.pdf
      https://cran.r-project.org/web/packages/movMF/index.html

    Basic sklearn scaffolding from sklearn.cluster.KMeans.

    Parameters
    ----------
    n_clusters : int, optional, default: 8
        The number of clusters to form as well as the number of
        centroids to generate.

    posterior_type: 'soft' or 'hard'
        Type of posterior computed in exepectation step.
        See note about attribute: self.posterior_

    force_weights : None or array [n_clusters, ]
        If None, the algorithm will estimate the weights.
        If an array of weights, algorithm will estimate concentrations and
        centers with given weights.

    max_iter : int, default: 300
        Maximum number of iterations of the k-means algorithm for a
        single run.

    n_init : int, default: 10
        Number of time the k-means algorithm will be run with different
        centroid seeds. The final results will be the best output of
        n_init consecutive runs in terms of inertia.

    init:  (string) one of
        random-class [default]: random class assignment & centroid computation
        k-means++ : uses sklearn k-means++ initialization algorithm
        spherical-k-means : use centroids from one pass of spherical k-means
        random : random unit norm vectors
        random-orthonormal : random orthonormal vectors
        If an ndarray is passed, it should be of shape (n_clusters, n_features)
        and gives the initial centers.

    tol : float, default: 1e-6
        Relative tolerance with regards to inertia to declare convergence

    n_jobs : int
        The number of jobs to use for the computation. This works by computing
        each of the n_init runs in parallel.
        If -1 all CPUs are used. If 1 is given, no parallel computing code is
        used at all, which is useful for debugging. For n_jobs below -1,
        (n_cpus + 1 + n_jobs) are used. Thus for n_jobs = -2, all CPUs but one
        are used.

    random_state : integer or numpy.RandomState, optional
        The generator used to initialize the centers. If an integer is
        given, it fixes the seed. Defaults to the global numpy random
        number generator.

    verbose : int, default 0
        Verbosity mode.

    copy_x : boolean, default True
        When pre-computing distances it is more numerically accurate to center
        the data first.  If copy_x is True, then the original data is not
        modified.  If False, the original data is modified, and put back before
        the function returns, but small numerical differences may be introduced
        by subtracting and then adding the data mean.

    normalize : boolean, default True
        Normalize the input to have unnit norm.

    Attributes
    ----------

    cluster_centers_ : array, [n_clusters, n_features]
        Coordinates of cluster centers

    labels_ :
        Labels of each point

    inertia_ : float
        Sum of distances of samples to their closest cluster center.

    weights_ : array, [n_clusters,]
        Weights of each cluster in vMF distribution (alpha).

    concentrations_ : array [n_clusters,]
        Concentration parameter for each cluster (kappa).
        Larger values correspond to more concentrated clusters.

    posterior_ : array, [n_clusters, n_examples]
        Each column corresponds to the posterio distribution for and example.

        If posterior_type='hard' is used, there will only be one non-zero per
        column, its index corresponding to the example's cluster label.

        If posterior_type='soft' is used, this matrix will be dense and the
        column values correspond to soft clustering weights.
    """

    def __init__(
        self,
        n_clusters=5,
        posterior_type="soft",
        force_weights=None,
        n_init=10,
        n_jobs=1,
        max_iter=300,
        verbose=False,
        init="random-class",
        random_state=None,
        tol=1e-6,
        copy_x=True,
        normalize=True,
    ):
        self.n_clusters = n_clusters
        self.posterior_type = posterior_type
        self.force_weights = force_weights
        self.n_init = n_init
        self.n_jobs = n_jobs
        self.max_iter = max_iter
        self.verbose = verbose
        self.init = init
        self.random_state = random_state
        self.tol = tol
        self.copy_x = copy_x
        self.normalize = normalize

    def _check_force_weights(self):
        if self.force_weights is None:
            return

        if len(self.force_weights) != self.n_clusters:
            raise ValueError(
                (
                    "len(force_weights)={} but must equal "
                    "n_clusters={}".format(len(self.force_weights), self.n_clusters)
                )
            )

    def _check_fit_data(self, X):
        """Verify that the number of samples given is larger than k"""
        X = check_array(X, accept_sparse="csr", dtype=[np.float64, np.float32])
        n_samples, n_features = X.shape
        if X.shape[0] < self.n_clusters:
            raise ValueError(
                "n_samples=%d should be >= n_clusters=%d"
                % (X.shape[0], self.n_clusters)
            )

        for ee in range(n_samples):
            if sp.issparse(X):
                n = sp.linalg.norm(X[ee, :])
            else:
                n = np.linalg.norm(X[ee, :])

            if np.abs(n - 1.) > 1e-4:
                raise ValueError("Data l2-norm must be 1, found {}".format(n))

        return X

    def _check_test_data(self, X):
        X = check_array(X, accept_sparse="csr", dtype=FLOAT_DTYPES)
        n_samples, n_features = X.shape
        expected_n_features = self.cluster_centers_.shape[1]
        if not n_features == expected_n_features:
            raise ValueError(
                "Incorrect number of features. "
                "Got %d features, expected %d" % (n_features, expected_n_features)
            )

        for ee in range(n_samples):
            if sp.issparse(X):
                n = sp.linalg.norm(X[ee, :])
            else:
                n = np.linalg.norm(X[ee, :])

            if np.abs(n - 1.) > 1e-4:
                raise ValueError("Data l2-norm must be 1, found {}".format(n))

        return X

    def fit(self, X, v_weights=None, add_uniform_cluster=False, random_weight_mod=1.0):
        """Compute mixture of von Mises Fisher clustering.

        Parameters
        ----------
        X : array-like or sparse matrix, shape=(n_samples, n_features)
        """
        if self.normalize:
            X = normalize(X)

        self._check_force_weights()
        random_state = check_random_state(self.random_state)
        X = self._check_fit_data(X)

        (
            self.cluster_centers_,
            self.labels_,
            self.inertia_,
            self.weights_,
            self.concentrations_,
            self.std_devs_,
            self.posterior_,
        ) = movMF(
            X,
            self.n_clusters,
            posterior_type=self.posterior_type,
            force_weights=self.force_weights,
            n_init=self.n_init,
            n_jobs=self.n_jobs,
            max_iter=self.max_iter,
            verbose=self.verbose,
            init=self.init,
            random_state=random_state,
            tol=self.tol,
            copy_x=self.copy_x,
            v_weights=v_weights,
            add_uniform_cluster=add_uniform_cluster,
            random_weight_mod=random_weight_mod,
        )

        return self

    def fit_predict(self, X, y=None):
        """Compute cluster centers and predict cluster index for each sample.
        Convenience method; equivalent to calling fit(X) followed by
        predict(X).
        """
        return self.fit(X).labels_

    def fit_transform(self, X, y=None):
        """Compute clustering and transform X to cluster-distance space.
        Equivalent to fit(X).transform(X), but more efficiently implemented.
        """
        # Currently, this just skips a copy of the data if it is not in
        # np.array or CSR format already.
        # XXX This skips _check_test_data, which may change the dtype;
        # we should refactor the input validation.
        return self.fit(X)._transform(X)

    def transform(self, X, y=None):
        """Transform X to a cluster-distance space.
        In the new space, each dimension is the cosine distance to the cluster
        centers.  Note that even if X is sparse, the array returned by
        `transform` will typically be dense.

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape = [n_samples, n_features]
            New data to transform.

        Returns
        -------
        X_new : array, shape [n_samples, k]
            X transformed in the new space.
        """
        if self.normalize:
            X = normalize(X)

        check_is_fitted(self, "cluster_centers_")
        X = self._check_test_data(X)
        return self._transform(X)

    def _transform(self, X):
        """guts of transform method; no input validation"""
        return cosine_distances(X, self.cluster_centers_)

    def predict(self, X):
        """Predict the closest cluster each sample in X belongs to.
        In the vector quantization literature, `cluster_centers_` is called
        the code book and each value returned by `predict` is the index of
        the closest code in the code book.

        Note:  Does not check that each point is on the sphere.

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape = [n_samples, n_features]
            New data to predict.

        Returns
        -------
        labels : array, shape [n_samples,]
            Index of the cluster each sample belongs to.
        """
        if self.normalize:
            X = normalize(X)

        check_is_fitted(self, "cluster_centers_")

        X = self._check_test_data(X)
        return _labels_inertia(X, self.cluster_centers_)[0]

    def score(self, X, y=None):
        """Inertia score (sum of all distances to closest cluster).

        Parameters
        ----------
        X : {array-like, sparse matrix}, shape = [n_samples, n_features]
            New data.

        Returns
        -------
        score : float
            Larger score is better.
        """
        if self.normalize:
            X = normalize(X)

        check_is_fitted(self, "cluster_centers_")
        X = self._check_test_data(X)
        return -_labels_inertia(X, self.cluster_centers_)[1]

    def log_likelihood(self, X):
        check_is_fitted(self, "cluster_centers_")

        return _log_likelihood(
            X, self.cluster_centers_, self.weights_, self.concentrations_
        )
    
    def log_likelihood_total(self, X):
        check_is_fitted(self, "cluster_centers_")

        return _log_likelihood_total(
            X, self.cluster_centers_, self.weights_, self.concentrations_
        )

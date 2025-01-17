"""
Base class for moment-constrained GW solvers with periodic boundary
conditions.
"""

import functools

import numpy as np
from pyscf import lib
from pyscf.lib import logger
from pyscf.pbc.mp.kmp2 import get_frozen_mask, get_nmo, get_nocc

from momentGW.base import BaseGW
from momentGW.pbc.kpts import KPoints


class BaseKGW(BaseGW):
    """{description}

    Parameters
    ----------
    mf : pyscf.pbc.scf.KSCF
        PySCF periodic mean-field class.
    diagonal_se : bool, optional
        If `True`, use a diagonal approximation in the self-energy.
        Default value is `False`.
    polarizability : str, optional
        Type of polarizability to use, can be one of `("drpa",
        "drpa-exact").  Default value is `"drpa"`.
    npoints : int, optional
        Number of numerical integration points.  Default value is `48`.
    optimise_chempot : bool, optional
        If `True`, optimise the chemical potential by shifting the
        position of the poles in the self-energy relative to those in
        the Green's function.  Default value is `False`.
    fock_loop : bool, optional
        If `True`, self-consistently renormalise the density matrix
        according to the updated Green's function.  Default value is
        `False`.
    fock_opts : dict, optional
        Dictionary of options compatiable with `pyscf.dfragf2.DFRAGF2`
        objects that are used in the Fock loop.
    compression : str, optional
        Blocks of the ERIs to use as a metric for compression. Can be
        one or more of `("oo", "ov", "vv", "ia")` which can be passed as
        a comma-separated string. `"oo"`, `"ov"` and `"vv"` refer to
        compression on the initial ERIs, whereas `"ia"` refers to
        compression on the ERIs entering RPA, which may change under a
        self-consistent scheme.  Default value is `"ia"`.
    compression_tol : float, optional
        Tolerance for the compression.  Default value is `1e-10`.
    {extra_parameters}
    """

    # --- Default KGW options

    compression = None

    # --- Extra PBC options

    fc = False

    _opts = BaseGW._opts + [
        "fc",
    ]

    def __init__(self, mf, **kwargs):
        self._scf = mf
        self.verbose = self.mol.verbose
        self.stdout = self.mol.stdout
        self.max_memory = 1e10

        for key, val in kwargs.items():
            if not hasattr(self, key):
                raise AttributeError("%s has no attribute %s", self.name, key)
            setattr(self, key, val)

        # Do not modify:
        self.mo_energy = np.asarray(mf.mo_energy)
        self.mo_coeff = np.asarray(mf.mo_coeff)
        self.mo_occ = np.asarray(mf.mo_occ)
        self.frozen = None
        self._nocc = None
        self._nmo = None
        self._kpts = KPoints(self.cell, getattr(mf, "kpts", np.zeros((1, 3))))
        self.converged = None
        self.se = None
        self.gf = None
        self._qp_energy = None

        self._keys = set(self.__dict__.keys()).union(self._opts)

    def kernel(
        self,
        nmom_max,
        mo_energy=None,
        mo_coeff=None,
        moments=None,
        integrals=None,
    ):
        if mo_coeff is None:
            mo_coeff = self.mo_coeff
        if mo_energy is None:
            mo_energy = self.mo_energy

        cput0 = (logger.process_clock(), logger.perf_counter())
        self.dump_flags()
        logger.info(self, "nmom_max = %d", nmom_max)

        self.converged, self.gf, self.se, self._qp_energy = self._kernel(
            nmom_max,
            mo_energy,
            mo_coeff,
            integrals=integrals,
        )

        gf_occ = self.gf[0].get_occupied()
        gf_occ.remove_uncoupled(tol=1e-1)
        for n in range(min(5, gf_occ.naux)):
            en = -gf_occ.energy[-(n + 1)]
            vn = gf_occ.coupling[:, -(n + 1)]
            qpwt = np.linalg.norm(vn) ** 2
            logger.note(self, "IP energy level (Γ) %d E = %.16g  QP weight = %0.6g", n, en, qpwt)

        gf_vir = self.gf[0].get_virtual()
        gf_vir.remove_uncoupled(tol=1e-1)
        for n in range(min(5, gf_vir.naux)):
            en = gf_vir.energy[n]
            vn = gf_vir.coupling[:, n]
            qpwt = np.linalg.norm(vn) ** 2
            logger.note(self, "EA energy level (Γ) %d E = %.16g  QP weight = %0.6g", n, en, qpwt)

        logger.timer(self, self.name, *cput0)

        return self.converged, self.gf, self.se, self.qp_energy

    @staticmethod
    def _gf_to_occ(gf):
        return tuple(BaseGW._gf_to_occ(g) for g in gf)

    @staticmethod
    def _gf_to_energy(gf):
        return tuple(BaseGW._gf_to_energy(g) for g in gf)

    @staticmethod
    def _gf_to_coupling(gf):
        return tuple(BaseGW._gf_to_coupling(g) for g in gf)

    def _gf_to_mo_energy(self, gf):
        """Find the poles of a GF which best overlap with the MOs.

        Parameters
        ----------
        gf : tuple of GreensFunction
            Green's function object.

        Returns
        -------
        mo_energy : ndarray
            Updated MO energies.
        """

        mo_energy = np.zeros_like(self.mo_energy)

        for k in self.kpts.loop(1):
            check = set()
            for i in range(self.nmo):
                arg = np.argmax(gf[k].coupling[i] * gf[k].coupling[i].conj())
                mo_energy[k][i] = gf[k].energy[arg]
                check.add(arg)

            if len(check) != self.nmo:
                logger.warn(self, f"Inconsistent quasiparticle weights at k-point {k}!")

        return mo_energy

    @property
    def cell(self):
        return self._scf.cell

    mol = cell

    get_nmo = get_nmo
    get_nocc = get_nocc
    get_frozen_mask = get_frozen_mask

    @property
    def kpts(self):
        return self._kpts

    @property
    def nkpts(self):
        return len(self.kpts)

    @property
    def nmo(self):
        nmo = self.get_nmo(per_kpoint=False)
        return nmo

    @property
    def nocc(self):
        nocc = self.get_nocc(per_kpoint=True)
        return nocc

"""
Construct TDA moments with periodic boundary conditions.
"""

import numpy as np
import scipy.special
from pyscf import lib
from pyscf.agf2 import mpi_helper

from momentGW.tda import TDA as MolTDA


class TDA(MolTDA):
    """
    Compute the self-energy moments using dTDA and numerical integration

    with periodic boundary conditions.
    Parameters
    ----------
    gw : BaseKGW
        GW object.
    nmom_max : int
        Maximum moment number to calculate.
    Lpx : numpy.ndarray
        Density-fitted ERI tensor, where the first two indices
        enumerate the k-points, the third index is the auxiliary
        basis function index, and the fourth and fifth indices are
        the MO and Green's function orbital indices, respectively.
    integrals : KIntegrals
        Density-fitted integrals.
    mo_energy : numpy.ndarray or tuple of numpy.ndarray, optional
        Molecular orbital energies at each k-point.  If a tuple is passed,
        the first element corresponds to the Green's function basis and
        the second to the screened Coulomb interaction.  Default value is
        that of `gw._scf.mo_energy`.
    mo_occ : numpy.ndarray or tuple of numpy.ndarray, optional
        Molecular orbital occupancies at each k-point.  If a tuple is
        passed, the first element corresponds to the Green's function basis
        and the second to the screened Coulomb interaction.  Default value
        is that of `gw._scf.mo_occ`.
    """

    def __init__(
        self,
        gw,
        nmom_max,
        integrals,
        mo_energy=None,
        mo_occ=None,
    ):
        self.gw = gw
        self.nmom_max = nmom_max
        self.integrals = integrals

        # Get the MO energies for G and W
        if mo_energy is None:
            self.mo_energy_g = self.mo_energy_w = gw._scf.mo_energy
        elif isinstance(mo_energy, tuple):
            self.mo_energy_g, self.mo_energy_w = mo_energy
        else:
            self.mo_energy_g = self.mo_energy_w = mo_energy

        # Get the MO occupancies for G and W
        if mo_occ is None:
            self.mo_occ_g = self.mo_occ_w = gw._scf.mo_occ
        elif isinstance(mo_occ, tuple):
            self.mo_occ_g, self.mo_occ_w = mo_occ
        else:
            self.mo_occ_g = self.mo_occ_w = mo_occ

        # Options and thresholds
        self.report_quadrature_error = True
        if self.gw.compression and "ia" in self.gw.compression.split(","):
            self.compression_tol = gw.compression_tol
        else:
            self.compression_tol = None

    def compress_eris(self):
        """Compress the ERI tensors."""

        return  # TODO

    def build_dd_moments(self):
        """Build the moments of the density-density response."""

        cput0 = (lib.logger.process_clock(), lib.logger.perf_counter())
        lib.logger.info(self.gw, "Building density-density moments")
        lib.logger.debug(self.gw, "Memory usage: %.2f GB", self._memory_usage())

        kpts = self.kpts
        moments = np.zeros((self.nkpts, self.nkpts, self.nmom_max + 1), dtype=object)

        # Get the zeroth order moment
        for (q, qpt), (kb, kptb) in kpts.loop(2):
            kj = kpts.member(kpts.wrap_around(kptb - qpt))
            moments[q, kb, 0] += self.integrals.Lia[kj, kb] / self.nkpts
        cput1 = lib.logger.timer(self.gw, "zeroth moment", *cput0)

        # Get the higher order moments
        for i in range(1, self.nmom_max + 1):
            for (q, qpt), (kb, kptb) in kpts.loop(2):
                kj = kpts.member(kpts.wrap_around(kptb - qpt))

                d = lib.direct_sum(
                    "a-i->ia",
                    self.mo_energy_w[kb][self.mo_occ_w[kb] == 0],
                    self.mo_energy_w[kj][self.mo_occ_w[kj] > 0],
                )
                moments[q, kb, i] += moments[q, kb, i - 1] * d.ravel()[None]

            for (q, qpt), (ka, kpta), (kb, kptb) in kpts.loop(3):
                ki = kpts.member(kpts.wrap_around(kpta - qpt))
                kj = kpts.member(kpts.wrap_around(kptb - qpt))

                moments[q, kb, i] += (
                    np.linalg.multi_dot(
                        (
                            moments[q, ka, i - 1],
                            self.integrals.Lia[ki, ka].T.conj(),  # NOTE missing conj in notes
                            self.integrals.Lai[kj, kb].conj(),
                        )
                    )
                    * 2.0
                    / self.nkpts
                )

            cput1 = lib.logger.timer(self.gw, "moment %d" % i, *cput1)

        return moments

    def build_se_moments(self, moments_dd):
        """Build the moments of the self-energy via convolution."""

        cput0 = (lib.logger.process_clock(), lib.logger.perf_counter())
        lib.logger.info(self.gw, "Building self-energy moments")
        lib.logger.debug(self.gw, "Memory usage: %.2f GB", self._memory_usage())

        # Setup dependent on diagonal SE
        if self.gw.diagonal_se:
            pqchar = charp = qchar = "p"
            eta_shape = lambda k: (self.mo_energy_g[k].size, self.nmom_max + 1, self.nmo)
            fproc = lambda x: np.diag(x)
        else:
            pqchar, pchar, qchar = "pq", "p", "q"
            eta_shape = lambda k: (self.mo_energy_g[k].size, self.nmom_max + 1, self.nmo, self.nmo)
            fproc = lambda x: x
        eta = np.zeros((self.nkpts, self.nkpts), dtype=object)

        # Get the moments in (aux|aux) and rotate to (mo|mo)
        for n in range(self.nmom_max + 1):
            for q, qpt in enumerate(self.kpts):
                eta_aux = 0
                for kb, kptb in enumerate(self.kpts):
                    kj = self.kpts.member(self.kpts.wrap_around(kptb - qpt))
                    eta_aux += np.dot(moments_dd[q, kb, n], self.integrals.Lia[kj, kb].T.conj())

                for kp, kptp in enumerate(self.kpts):
                    kx = self.kpts.member(self.kpts.wrap_around(kptp - qpt))

                    if not isinstance(eta[kp, q], np.ndarray):
                        eta[kp, q] = np.zeros(eta_shape(kx), dtype=eta_aux.dtype)

                    for x in range(self.mo_energy_g[kx].size):
                        Lp = self.integrals.Lpx[kp, kx][:, :, x]
                        eta[kp, q][x, n] += (
                            lib.einsum(f"P{pchar},Q{qchar},PQ->{pqchar}", Lp, Lp.conj(), eta_aux)
                            * 2.0
                            / self.nkpts
                        )
        cput1 = lib.logger.timer(self.gw, "rotating DD moments", *cput0)

        # Construct the self-energy moments
        moments_occ = np.zeros((self.nkpts, self.nmom_max + 1, self.nmo, self.nmo), dtype=complex)
        moments_vir = np.zeros((self.nkpts, self.nmom_max + 1, self.nmo, self.nmo), dtype=complex)
        moms = np.arange(self.nmom_max + 1)
        for n in moms:
            fp = scipy.special.binom(n, moms)
            fh = fp * (-1) ** moms
            for (q, qpt), (kp, kptp) in self.kpts.loop(2):
                kx = self.kpts.member(self.kpts.wrap_around(kptp - qpt))

                eo = np.power.outer(self.mo_energy_g[kx][self.mo_occ_g[kx] > 0], n - moms)
                to = lib.einsum(
                    f"t,kt,kt{pqchar}->{pqchar}", fh, eo, eta[kp, q][self.mo_occ_g[kx] > 0]
                )
                moments_occ[kp, n] += fproc(to)

                ev = np.power.outer(self.mo_energy_g[kx][self.mo_occ_g[kx] == 0], n - moms)
                tv = lib.einsum(
                    f"t,ct,ct{pqchar}->{pqchar}", fp, ev, eta[kp, q][self.mo_occ_g[kx] == 0]
                )
                moments_vir[kp, n] += fproc(tv)

        for k, kpt in enumerate(self.kpts):
            for n in range(self.nmom_max + 1):
                if not np.allclose(moments_occ[k, n], moments_occ[k, n].T.conj()):
                    np.set_printoptions(edgeitems=1000, linewidth=1000, precision=4)
                    print(moments_occ[k, n])
                if not np.allclose(moments_occ[k, n], moments_occ[k, n].T.conj()):
                    raise ValueError("moments_occ not hermitian")
                if not np.allclose(moments_vir[k, n], moments_vir[k, n].T.conj()):
                    raise ValueError("moments_vir not hermitian")
                moments_occ[k, n] = 0.5 * (moments_occ[k, n] + moments_occ[k, n].T.conj())
                moments_vir[k, n] = 0.5 * (moments_vir[k, n] + moments_vir[k, n].T.conj())

        cput1 = lib.logger.timer(self.gw, "constructing SE moments", *cput1)

        return moments_occ, moments_vir

    def build_dd_moments_exact(self):
        raise NotImplementedError

    @property
    def naux(self):
        """Number of auxiliaries."""
        return self.integrals.naux

    @property
    def nov(self):
        """Number of ov states in W."""
        return np.multiply.outer(
            [np.sum(occ > 0) for occ in self.mo_occ_w],
            [np.sum(occ == 0) for occ in self.mo_occ_w],
        )

    @property
    def kpts(self):
        """k-points."""
        return self.gw.kpts

    @property
    def nkpts(self):
        """Number of k-points."""
        return self.gw.nkpts

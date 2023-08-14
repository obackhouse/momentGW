"""
Spin-restricted quasiparticle self-consistent GW via self-energy moment
constraints for molecular systems.
"""

import numpy as np
from pyscf import lib
from pyscf.agf2 import GreensFunction, mpi_helper
from pyscf.agf2.dfragf2 import get_jk
from pyscf.ao2mo import _ao2mo
from pyscf.lib import logger

from momentGW import util
from momentGW.base import BaseGW
from momentGW.evgw import evGW
from momentGW.gw import GW
from momentGW.ints import Integrals


def kernel(
    gw,
    nmom_max,
    mo_energy,
    mo_coeff,
    moments=None,
    integrals=None,
):
    """
    Moment-constrained quasiparticle self-consistent GW.

    Parameters
    ----------
    gw : BaseGW
        GW object.
    nmom_max : int
        Maximum moment number to calculate.
    mo_energy : numpy.ndarray
        Molecular orbital energies.
    mo_coeff : numpy.ndarray
        Molecular orbital coefficients.
    moments : tuple of numpy.ndarray, optional
        Tuple of (hole, particle) moments, if passed then they will
        be used  as the initial guess instead of calculating them.
        Default value is None.
    integrals : Integrals, optional
        Density-fitted integrals. If None, generate from scratch.
        Default value is None.

    Returns
    -------
    conv : bool
        Convergence flag.
    gf : pyscf.agf2.GreensFunction
        Green's function object
    se : pyscf.agf2.SelfEnergy
        Self-energy object
    """

    logger.warn(gw, "qsGW is untested!")

    if gw.polarizability == "drpa-exact":
        raise NotImplementedError("%s for polarizability=%s" % (gw.name, gw.polarizability))

    if integrals is None:
        integrals = gw.ao2mo()

    mo_energy = mo_energy.copy()
    mo_energy_ref = mo_energy.copy()
    mo_coeff = mo_coeff.copy()
    mo_coeff_ref = mo_coeff.copy()

    # Get the overlap
    ovlp = gw._scf.get_ovlp()
    sc = ovlp @ mo_coeff

    # Get the density matrix
    dm = gw._scf.make_rdm1(mo_coeff)
    dm = sc.swapaxes(-1, -2) @ dm @ sc

    # Get the core Hamiltonian
    h1e = gw._scf.get_hcore()
    h1e = mo_coeff.swapaxes(-1, -2) @ h1e @ mo_coeff

    diis = util.DIIS()
    diis.space = gw.diis_space

    # Get the self-energy
    subgw = gw.solver(gw._scf, **(gw.solver_options if gw.solver_options else {}))
    subgw.verbose = 0
    subgw.mo_energy = mo_energy
    subgw.mo_coeff = mo_coeff
    subconv, gf, se = subgw.kernel(nmom_max=nmom_max, integrals=integrals)

    # Get the moments
    th, tp = gw.self_energy_to_moments(se, nmom_max)

    conv = False
    for cycle in range(1, gw.max_cycle + 1):
        logger.info(gw, "%s iteration %d", gw.name, cycle)

        # Build the static potential
        se_qp = gw.build_static_potential(mo_energy, se)
        se_qp = gw.project_basis(se_qp, ovlp, mo_coeff, mo_coeff_ref)
        se_qp = diis.update(se_qp)

        # Update the MO energies and orbitals - essentially a Fock
        # loop using the folded static self-energy.
        conv_qp = False
        diis_qp = util.DIIS()
        diis_qp.space = gw.diis_space_qp
        mo_energy_prev = mo_energy.copy()
        for qp_cycle in range(1, gw.max_cycle_qp + 1):
            fock = integrals.get_fock(dm, h1e)
            fock_eff = fock + se_qp
            fock_eff = diis_qp.update(fock_eff)
            fock_eff = mpi_helper.bcast(fock_eff, root=0)

            mo_energy, u = np.linalg.eigh(fock_eff)
            u = mpi_helper.bcast(u, root=0)
            mo_coeff = mo_coeff_ref @ u

            dm_prev = dm
            dm = gw._scf.make_rdm1(u)
            error = np.max(np.abs(dm - dm_prev))
            if error < gw.conv_tol_qp:
                conv_qp = True
                break

        if conv_qp:
            logger.info(gw, "QP loop converged.")
        else:
            logger.info(gw, "QP loop failed to converge.")

        # Update the self-energy
        subgw.mo_energy = mo_energy
        subgw.mo_coeff = mo_coeff
        _, gf, se = subgw.kernel(nmom_max=nmom_max)

        # Update the moments
        th_prev, tp_prev = th, tp
        th, tp = gw.self_energy_to_moments(se, nmom_max)
        th = gw.project_basis(th, ovlp, mo_coeff, mo_coeff_ref)
        tp = gw.project_basis(tp, ovlp, mo_coeff, mo_coeff_ref)

        # Check for convergence
        conv = gw.check_convergence(mo_energy, mo_energy_prev, th, th_prev, tp, tp_prev)
        th_prev = th.copy()
        tp_prev = tp.copy()
        if conv:
            break

    return conv, gf, se, mo_energy


class qsGW(GW):
    __doc__ = BaseGW.__doc__.format(
        description="Spin-restricted quasiparticle self-consistent GW via self-energy moment constraints for molecules.",
        extra_parameters="""max_cycle : int, optional
        Maximum number of iterations.  Default value is 50.
    max_cycle_qp : int, optional
        Maximum number of iterations in the quasiparticle equation
        loop.  Default value is 50.
    conv_tol : float, optional
        Convergence threshold in the change in the HOMO and LUMO.
        Default value is 1e-8.
    conv_tol_moms : float, optional
        Convergence threshold in the change in the moments. Default
        value is 1e-8.
    conv_tol_qp : float, optional
        Convergence threshold in the change in the density matrix in
        the quasiparticle equation loop.  Default value is 1e-8.
    conv_logical : callable, optional
        Function that takes an iterable of booleans as input indicating
        whether the individual `conv_tol`, `conv_tol_moms`,
        `conv_tol_qp` have been satisfied, respectively, and returns a
        boolean indicating overall convergence. For example, the
        function `all` requires both metrics to be met, and `any`
        requires just one. Default value is `all`.
    diis_space : int, optional
        Size of the DIIS extrapolation space.  Default value is 8.
    diis_space_qp : int, optional
        Size of the DIIS extrapolation space in the quasiparticle
        loop.  Default value is 8.
    eta : float, optional
        Small value to regularise the self-energy.  Default value is
        `1e-1`.
    srg : float, optional
        If non-zero, use the similarity renormalisation group approach
        of Marie and Loos in place of the `eta` regularisation.  For
        value recommendations refer to their paper (arXiv:2303.05984).
        Default value is `0.0`.
    solver : BaseGW, optional
        Solver to use to obtain the self-energy.  Compatible with any
        `BaseGW`-like class.  Default value is `momentGW.gw.GW`.
    solver_options : dict, optional
        Keyword arguments to pass to the solver.  Default value is an
        emtpy `dict`.
    """,
    )

    # --- Extra qsGW options

    max_cycle = 50
    max_cycle_qp = 50
    conv_tol = 1e-8
    conv_tol_moms = 1e-6
    conv_tol_qp = 1e-8
    conv_logical = all
    diis_space = 8
    diis_space_qp = 8
    eta = 1e-1
    srg = 0.0
    solver = GW
    solver_options = None

    _opts = GW._opts + [
        "max_cycle",
        "max_cycle_qp",
        "conv_tol",
        "conv_tol_moms",
        "conv_tol_qp",
        "conv_logical",
        "diis_space",
        "diis_space_qp",
        "eta",
        "srg",
        "solver",
        "solver_options",
    ]

    @property
    def name(self):
        return "qsGW"

    @staticmethod
    def project_basis(matrix, ovlp, mo1, mo2):
        """
        Project a matrix from one basis to another.

        Parameters
        ----------
        matrix : numpy.ndarray
            Matrix to project.
        ovlp : numpy.ndarray
            Overlap matrix in the shared (AO) basis.
        mo1 : numpy.ndarray
            First basis, rotates from the shared (AO) basis into the
            basis of `matrix`.
        mo2 : numpy.ndarray
            Second basis, rotates from the shared (AO) basis into the
            desired basis of the output.

        Returns
        -------
        projected_matrix : numpy.ndarray
            Matrix projected into the desired basis.
        """
        proj = np.linalg.multi_dot((mo1.T, ovlp, mo2))
        return lib.einsum("...pq,pi,qj->...ij", matrix, proj, proj)

    @staticmethod
    def self_energy_to_moments(se, nmom_max):
        """
        Return the hole and particle moments for a self-energy.

        Parameters
        ----------
        se : SelfEnergy
            Self-energy to compute the moments of.

        Returns
        -------
        th : numpy.ndarray
            Hole moments.
        tp : numpy.ndarray
            Particle moments.
        """
        th = se.get_occupied().moment(range(nmom_max + 1))
        tp = se.get_virtual().moment(range(nmom_max + 1))
        return th, tp

    def build_static_potential(self, mo_energy, se):
        """
        Build the static potential approximation to the self-energy.

        Parameters
        ----------
        mo_energy : numpy.ndarray
            Molecular orbital energies.
        se : SelfEnergy
            Self-energy to approximate.

        Returns
        se_qp : numpy.ndarray
            Static potential approximation to the self-energy.
        """

        if self.srg == 0.0:
            eta = np.sign(se.energy) * self.eta * 1.0j
            denom = lib.direct_sum("p-q-q->pq", mo_energy, se.energy, eta)
            se_qp = lib.einsum("pk,qk,pk->pq", se.coupling, se.coupling, 1 / denom).real
        else:
            denom = lib.direct_sum("p-q->pq", mo_energy, se.energy)
            d2p = lib.direct_sum("pk,qk->pqk", denom**2, denom**2)
            reg = 1 - np.exp(-d2p * self.srg)
            reg *= lib.direct_sum("pk,qk->pqk", denom, denom)
            reg /= d2p
            se_qp = lib.einsum("pk,qk,pqk->pq", se.coupling, se.coupling, reg).real

        se_qp = 0.5 * (se_qp + se_qp.T)

        return se_qp

    check_convergence = evGW.check_convergence

    def kernel(
        self,
        nmom_max,
        mo_energy=None,
        mo_coeff=None,
        moments=None,
        integrals=None,
    ):
        if mo_coeff is None:
            mo_coeff = self._scf.mo_coeff
        if mo_energy is None:
            mo_energy = self._scf.mo_energy

        cput0 = (logger.process_clock(), logger.perf_counter())
        self.dump_flags()
        logger.info(self, "nmom_max = %d", nmom_max)

        self.converged, self.gf, self.se, self._qp_energy = kernel(
            self,
            nmom_max,
            mo_energy,
            mo_coeff,
            integrals=integrals,
        )

        gf_occ = self.gf.get_occupied()
        gf_occ.remove_uncoupled(tol=1e-1)
        for n in range(min(5, gf_occ.naux)):
            en = -gf_occ.energy[-(n + 1)]
            vn = gf_occ.coupling[:, -(n + 1)]
            qpwt = np.linalg.norm(vn) ** 2
            logger.note(self, "IP energy level %d E = %.16g  QP weight = %0.6g", n, en, qpwt)

        gf_vir = self.gf.get_virtual()
        gf_vir.remove_uncoupled(tol=1e-1)
        for n in range(min(5, gf_vir.naux)):
            en = gf_vir.energy[n]
            vn = gf_vir.coupling[:, n]
            qpwt = np.linalg.norm(vn) ** 2
            logger.note(self, "EA energy level %d E = %.16g  QP weight = %0.6g", n, en, qpwt)

        if self.converged:
            logger.note(self, "%s converged", self.name)
        else:
            logger.note(self, "%s failed to converge", self.name)

        logger.timer(self, self.name, *cput0)

        return self.converged, self.gf, self.se

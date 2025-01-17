"""
Integral helpers with periodic boundary conditions.
"""

from collections import defaultdict

import numpy as np
from pyscf import lib
from pyscf.agf2 import mpi_helper
from pyscf.lib import logger
from pyscf.pbc import tools

from momentGW.ints import Integrals


class KIntegrals(Integrals):
    """
    Container for the integrals required for KGW methods.
    """

    def __init__(
        self,
        with_df,
        kpts,
        mo_coeff,
        mo_occ,
        compression="ia",
        compression_tol=1e-10,
        store_full=False,
    ):
        Integrals.__init__(
            self,
            with_df,
            mo_coeff,
            mo_occ,
            compression=compression,
            compression_tol=compression_tol,
            store_full=store_full,
        )

        self.kpts = kpts

        self._madelung = None

    def get_compression_metric(self):
        """
        Return the compression metric.
        """

        # TODO MPI

        compression = self._parse_compression()
        if not compression:
            return None

        cput0 = (logger.process_clock(), logger.perf_counter())
        logger.info(self, f"Computing compression metric for {self.__class__.__name__}")

        prod = np.zeros((len(self.kpts), self.naux_full, self.naux_full), dtype=complex)

        # Loop over required blocks
        for key in sorted(compression):
            logger.debug(self, f"Transforming {key} block")
            ci, cj = [
                {
                    "o": [c[:, o > 0] for c, o in zip(self.mo_coeff, self.mo_occ)],
                    "v": [c[:, o == 0] for c, o in zip(self.mo_coeff, self.mo_occ)],
                    "i": [c[:, o > 0] for c, o in zip(self.mo_coeff_w, self.mo_occ_w)],
                    "a": [c[:, o == 0] for c, o in zip(self.mo_coeff_w, self.mo_occ_w)],
                }[k]
                for k in key
            ]
            ni = [c.shape[-1] for c in ci]
            nj = [c.shape[-1] for c in cj]

            for q, ki in self.kpts.loop(2):
                kj = self.kpts.member(self.kpts.wrap_around(self.kpts[ki] - self.kpts[q]))

                Lxy = np.zeros((self.naux_full, ni[ki] * nj[kj]), dtype=complex)
                b1 = 0
                for block in self.with_df.sr_loop((ki, kj), compact=False):
                    if block[2] == -1:
                        raise NotImplementedError("Low dimensional integrals")
                    block = block[0] + block[1] * 1.0j
                    block = block.reshape(self.naux_full, self.nmo, self.nmo)
                    b0, b1 = b1, b1 + block.shape[0]
                    logger.debug(self, f"  Block [{ki}, {kj}, {b0}:{b1}]")

                    tmp = lib.einsum("Lpq,pi,qj->Lij", block, ci[ki].conj(), cj[kj])
                    tmp = tmp.reshape(b1 - b0, -1)
                    Lxy[b0:b1] = tmp

                prod[q] += np.dot(Lxy, Lxy.T.conj()) / len(self.kpts)

        rot = np.empty((len(self.kpts),), dtype=object)
        if mpi_helper.rank == 0:
            for q in self.kpts.loop(1):
                e, v = np.linalg.eigh(prod[q])
                mask = np.abs(e) > self.compression_tol
                rot[q] = v[:, mask]
        else:
            for q in self.kpts.loop(1):
                rot[q] = np.zeros((0,), dtype=complex)
        del prod

        for q in self.kpts.loop(1):
            rot[q] = mpi_helper.bcast(rot[q], root=0)

            if rot[q].shape[-1] == self.naux_full:
                logger.info(self, f"No compression found at q-point {q}")
                rot[q] = None
            else:
                logger.info(
                    self,
                    f"Compressed auxiliary space from {self.naux_full} to {rot[q].shape[-1]} and q-point {q}",
                )
        logger.timer(self, "compression metric", *cput0)

        return rot

    def transform(self, do_Lpq=None, do_Lpx=True, do_Lia=True):
        """
        Initialise the integrals, building:
            - Lpq: the full (aux, MO, MO) array if `store_full`
            - Lpx: the compressed (aux, MO, MO) array
            - Lia: the compressed (aux, occ, vir) array
        """

        # Get the compression metric
        if self._rot is None:
            self._rot = self.get_compression_metric()
        rot = self._rot
        if rot is None:
            eye = np.eye(self.naux_full)
            rot = defaultdict(lambda: eye)
        for q in self.kpts.loop(1):
            if rot[q] is None:
                rot[q] = np.eye(self.naux_full)

        do_Lpq = self.store_full if do_Lpq is None else do_Lpq
        if not any([do_Lpq, do_Lpx, do_Lia]):
            return

        cput0 = (logger.process_clock(), logger.perf_counter())
        logger.info(self, f"Transforming {self.__class__.__name__}")

        Lpq = {}
        Lpx = {}
        Lia = {}
        Lai = {}

        for q in self.kpts.loop(1):
            for ki in self.kpts.loop(1, mpi=True):
                kj = self.kpts.member(self.kpts.wrap_around(self.kpts[q] + self.kpts[ki]))

                # Get the slices on the current process and initialise the arrays
                Lpq_k = (
                    np.zeros((self.naux_full, self.nmo, self.nmo), dtype=complex)
                    if do_Lpq
                    else None
                )
                Lpx_k = (
                    np.zeros((self.naux[q], self.nmo, self.nmo_g[kj]), dtype=complex)
                    if do_Lpx
                    else None
                )
                Lia_k = (
                    np.zeros((self.naux[q], self.nocc_w[ki] * self.nvir_w[kj]), dtype=complex)
                    if do_Lia
                    else None
                )
                Lai_k = (
                    np.zeros((self.naux[q], self.nocc_w[kj] * self.nvir_w[ki]), dtype=complex)
                    if do_Lia
                    else None
                )

                # Build the integrals blockwise
                b1 = 0
                for block in self.with_df.sr_loop((ki, kj), compact=False):  # TODO lock I/O
                    if block[2] == -1:
                        raise NotImplementedError("Low dimensional integrals")
                    block = block[0] + block[1] * 1.0j
                    block = block.reshape(self.naux_full, self.nmo, self.nmo)
                    b0, b1 = b1, b1 + block.shape[0]
                    logger.debug(self, f"  Block [{ki}, {kj}, {b0}:{b1}]")

                    # If needed, rotate the full (L|pq) array
                    if do_Lpq:
                        logger.debug(
                            self, f"(L|pq) size: ({self.naux_full}, {self.nmo}, {self.nmo})"
                        )
                        coeffs = (self.mo_coeff[ki], self.mo_coeff[kj])
                        Lpq_k[b0:b1] = lib.einsum(
                            "Lpq,pi,qj->Lij", block, coeffs[0].conj(), coeffs[1]
                        )

                    # Compress the block
                    block_comp = lib.einsum("L...,LQ->Q...", block, rot[q][b0:b1].conj())

                    # Build the compressed (L|px) array
                    if do_Lpx:
                        logger.debug(
                            self, f"(L|px) size: ({self.naux[q]}, {self.nmo}, {self.nmo_g[ki]})"
                        )
                        coeffs = (self.mo_coeff[ki], self.mo_coeff_g[kj])
                        Lpx_k += lib.einsum(
                            "Lpq,pi,qj->Lij", block_comp, coeffs[0].conj(), coeffs[1]
                        )

                    # Build the compressed (L|ia) array
                    if do_Lia:
                        logger.debug(
                            self,
                            f"(L|ia) size: ({self.naux[q]}, {self.nocc_w[ki] * self.nvir_w[kj]})",
                        )
                        coeffs = (
                            self.mo_coeff_w[ki][:, : self.nocc_w[ki]],
                            self.mo_coeff_w[kj][:, self.nocc_w[kj] :],
                        )
                        tmp = lib.einsum("Lpq,pi,qj->Lij", block_comp, coeffs[0].conj(), coeffs[1])
                        tmp = tmp.reshape(self.naux[q], -1)
                        Lia_k += tmp

                if do_Lpq:
                    Lpq[ki, kj] = Lpq_k
                if do_Lpx:
                    Lpx[ki, kj] = Lpx_k
                if do_Lia:
                    Lia[ki, kj] = Lia_k
                else:
                    continue

                # Inverse q for ki <-> kj
                q = self.kpts.member(self.kpts.wrap_around(-self.kpts[q]))

                # Build the integrals blockwise
                b1 = 0
                for block in self.with_df.sr_loop((kj, ki), compact=False):  # TODO lock I/O
                    if block[2] == -1:
                        raise NotImplementedError("Low dimensional integrals")
                    block = block[0] + block[1] * 1.0j
                    block = block.reshape(self.naux_full, self.nmo, self.nmo)
                    b0, b1 = b1, b1 + block.shape[0]
                    logger.debug(self, f"  Block [{ki}, {kj}, {b0}:{b1}]")

                    # Compress the block
                    block_comp = lib.einsum("L...,LQ->Q...", block, rot[q][b0:b1].conj())

                    # Build the compressed (L|ai) array
                    logger.debug(
                        self, f"(L|ai) size: ({self.naux[q]}, {self.nvir_w[kj] * self.nocc_w[ki]})"
                    )
                    coeffs = (
                        self.mo_coeff_w[kj][:, self.nocc_w[kj] :],
                        self.mo_coeff_w[ki][:, : self.nocc_w[ki]],
                    )
                    tmp = lib.einsum("Lpq,pi,qj->Lij", block_comp, coeffs[0].conj(), coeffs[1])
                    tmp = tmp.swapaxes(1, 2)
                    tmp = tmp.reshape(self.naux[q], -1)
                    Lai_k += tmp

                Lai[ki, kj] = Lai_k

        if do_Lpq:
            self._blocks["Lpq"] = Lpq
        if do_Lpx:
            self._blocks["Lpx"] = Lpx
        if do_Lia:
            self._blocks["Lia"] = Lia
            self._blocks["Lai"] = Lai

        logger.timer(self, "transform", *cput0)

    def get_j(self, dm, basis="mo"):
        """Build the J matrix."""

        assert basis in ("ao", "mo")

        vj = np.zeros_like(dm, dtype=complex)

        if self.store_full and basis == "mo":
            buf = 0.0
            for kk in self.kpts.loop(1, mpi=True):
                buf += lib.einsum("Lpq,pq->L", self.Lpq[kk, kk], dm[kk].conj())

            buf = mpi_helper.allreduce(buf)

            for ki in self.kpts.loop(1, mpi=True):
                vj[ki] += lib.einsum("Lpq,L->pq", self.Lpq[ki, ki], buf)

            vj = mpi_helper.allreduce(vj)

        else:
            if basis == "mo":
                dm = lib.einsum("kij,kpi,kqj->kpq", dm, self.mo_coeff, np.conj(self.mo_coeff))

            buf = np.zeros((self.naux_full,), dtype=complex)

            for kk in self.kpts.loop(1, mpi=True):
                b1 = 0
                for block in self.with_df.sr_loop((kk, kk), compact=False):  # TODO lock I/O
                    if block[2] == -1:
                        raise NotImplementedError("Low dimensional integrals")
                    block = block[0] + block[1] * 1.0j
                    block = block.reshape(self.naux_full, self.nmo, self.nmo)
                    b0, b1 = b1, b1 + block.shape[0]
                    buf[b0:b1] += lib.einsum("Lpq,pq->L", block, dm[kk].conj())

            buf = mpi_helper.allreduce(buf)

            for ki in self.kpts.loop(1, mpi=True):
                b1 = 0
                for block in self.with_df.sr_loop((ki, ki), compact=False):
                    if block[2] == -1:
                        raise NotImplementedError("Low dimensional integrals")
                    block = block[0] + block[1] * 1.0j
                    block = block.reshape(self.naux_full, self.nmo, self.nmo)
                    b0, b1 = b1, b1 + block.shape[0]
                    vj[ki] += lib.einsum("Lpq,L->pq", block, buf[b0:b1])

            vj = mpi_helper.allreduce(vj)

            if basis == "mo":
                vj = lib.einsum("kpq,kpi,kqj->kij", vj, np.conj(self.mo_coeff), self.mo_coeff)

        vj /= len(self.kpts)

        return vj

    def get_k(self, dm, basis="mo", ewald=False):
        """Build the K matrix."""

        assert basis in ("ao", "mo")

        vk = np.zeros_like(dm, dtype=complex)

        if self.store_full and basis == "mo":
            # TODO is there a better way to distribute this?
            for p0, p1 in lib.prange(0, self.naux_full, 240):
                buf = np.zeros(
                    (len(self.kpts), len(self.kpts), p1 - p0, self.nmo, self.nmo), dtype=complex
                )
                for ki in self.kpts.loop(1, mpi=True):
                    for kk in self.kpts.loop(1):
                        buf[kk, ki] = lib.einsum("Lpq,qr->Lrp", self.Lpq[ki, kk][p0:p1], dm[kk])

                buf = mpi_helper.allreduce(buf)

                for ki in self.kpts.loop(1):
                    for kk in self.kpts.loop(1, mpi=True):
                        vk[ki] += lib.einsum("Lrp,Lrs->ps", buf[kk, ki], self.Lpq[kk, ki][p0:p1])

            vk = mpi_helper.allreduce(vk)

        else:
            if basis == "mo":
                dm = lib.einsum("kij,kpi,kqj->kpq", dm, self.mo_coeff, np.conj(self.mo_coeff))

            for kk in self.kpts.loop(1):
                buf = np.zeros((len(self.kpts), self.naux_full, self.nmo, self.nmo), dtype=complex)
                for ki in self.kpts.loop(1, mpi=True):
                    b1 = 0
                    for block in self.with_df.sr_loop((ki, kk), compact=False):
                        if block[2] == -1:
                            raise NotImplementedError("Low dimensional integrals")
                        block = block[0] + block[1] * 1.0j
                        block = block.reshape(self.naux_full, self.nmo, self.nmo)
                        b0, b1 = b1, b1 + block.shape[0]
                        buf[ki, b0:b1] = lib.einsum("Lpq,qr->Lrp", block, dm[kk])

                buf = mpi_helper.allreduce(buf)

                for ki in self.kpts.loop(1, mpi=True):
                    b1 = 0
                    for block in self.with_df.sr_loop((kk, ki), compact=False):
                        if block[2] == -1:
                            raise NotImplementedError("Low dimensional integrals")
                        block = block[0] + block[1] * 1.0j
                        block = block.reshape(self.naux_full, self.nmo, self.nmo)
                        b0, b1 = b1, b1 + block.shape[0]
                        vk[ki] += lib.einsum("Lrp,Lrs->ps", buf[ki, b0:b1], block)

            vk = mpi_helper.allreduce(vk)

            if basis == "mo":
                vk = lib.einsum("kpq,kpi,kqj->kij", vk, np.conj(self.mo_coeff), self.mo_coeff)

        vk /= len(self.kpts)

        if ewald:
            vk += self.get_ewald(dm, basis=basis)

        return vk

    def get_ewald(self, dm, basis="mo"):
        """Build the Ewald exchange divergence matrix."""

        assert basis in ("ao", "mo")

        if basis == "mo":
            ovlp = defaultdict(lambda: np.eye(self.nmo))
        else:
            ovlp = self.with_df.cell.pbc_intor("int1e_ovlp", hermi=1, kpts=self.kpts._kpts)

        ew = lib.einsum("kpq,kpi,kqj->kij", dm, ovlp.conj(), ovlp)

        return ew

    @property
    def madelung(self):
        """
        Return the Madelung constant for the lattice.
        """
        if self._madelung is None:
            self._madeling = tools.pbc.madelung(self.with_df.cell, self.kpts._kpts)
        return self._madelung

    @property
    def Lai(self):
        """
        Return the compressed (aux, W vir, W occ) array.
        """
        return self._blocks["Lai"]

    @property
    def nmo(self):
        """
        Return the number of MOs.
        """
        assert len({c.shape[-1] for c in self.mo_coeff}) == 1
        return self.mo_coeff[0].shape[-1]

    @property
    def nocc(self):
        """
        Return the number of occupied MOs.
        """
        return [np.sum(o > 0) for o in self.mo_occ]

    @property
    def nvir(self):
        """
        Return the number of virtual MOs.
        """
        return [np.sum(o == 0) for o in self.mo_occ]

    @property
    def nmo_g(self):
        """
        Return the number of MOs for the Green's function.
        """
        return [c.shape[-1] for c in self.mo_coeff_g]

    @property
    def nmo_w(self):
        """
        Return the number of MOs for the screened Coulomb interaction.
        """
        return [c.shape[-1] for c in self.mo_coeff_w]

    @property
    def nocc_w(self):
        """
        Return the number of occupied MOs for the screened Coulomb
        interaction.
        """
        return [np.sum(o > 0) for o in self.mo_occ_w]

    @property
    def nvir_w(self):
        """
        Return the number of virtual MOs for the screened Coulomb
        interaction.
        """
        return [np.sum(o == 0) for o in self.mo_occ_w]

    @property
    def naux(self):
        """
        Return the number of auxiliary basis functions, after the
        compression.
        """
        if self._rot is None:
            return [self.naux_full] * len(self.kpts)
        return [c.shape[-1] if c is not None else self.naux_full for c in self._rot]

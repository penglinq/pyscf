#!/usr/bin/env python
#
# Author: Qiming Sun <osirpt.sun@gmail.com>
#

'''
domain decomposition COSMO
(In testing)

See also
https://github.com/filippolipparini/ddPCM
JCP, 141, 184108
JCTC, 9, 3637
'''

import ctypes
import numpy
from pyscf import lib
from pyscf.lib import logger
from pyscf import gto
from pyscf import df
from pyscf.dft import gen_grid, numint
from pyscf.data import elements

def ddcosmo_for_scf(mf, pcmobj):
    oldMF = mf.__class__
    cosmo_solver = pcmobj.as_solver()

    class MF(oldMF):
        def __init__(self):
            pass

        def dump_flags(self):
            oldMF.dump_flags(self)
            pcmobj.dump_flags()
            return self

        def get_veff(self, mol, dm, *args, **kwargs):
            vhf = oldMF.get_veff(self, mol, dm)
            epcm, vpcm = cosmo_solver(dm)
            vhf += vpcm
            return lib.tag_array(vhf, epcm=epcm, vpcm=vpcm)

        def energy_elec(self, dm=None, h1e=None, vhf=None):
            if dm is None:
                dm = self.make_rdm1()
            if getattr(vhf, 'epcm', None) is None:
                vhf = self.get_veff(self.mol, dm)
            e_tot, e_coul = oldMF.energy_elec(self, dm, h1e, vhf-vhf.vpcm)
            e_tot += vhf.epcm
            logger.info(pcmobj, '  E_diel = %.15g', vhf.epcm)
            return e_tot, e_coul

    mf1 = MF()
    mf1.__dict__.update(mf.__dict__)
    return mf1


# Generate ddcosmo function to compute energy and potential matrix
def gen_ddcosmo_solver(pcmobj, grids=None, verbose=None):
    mol = pcmobj.mol
    if grids is None:
        grids = gen_grid.Grids(mol)
        grids.level = pcmobj.becke_grids_level
        grids.coords, grids.weights = grids.build(with_non0tab=True)

    natm = mol.natm
    lmax = pcmobj.lmax
    atomic_radii = pcmobj.atomic_radii

    r_vdw = numpy.asarray([atomic_radii[gto.mole._charge(mol.atom_symbol(i))]
                           for i in range(natm)])
    coords_1sph, weights_1sph = make_grids_one_sphere(pcmobj.lebedev_order)
    ylm_1sph = numpy.vstack(make_ylm(coords_1sph, lmax))

    fi = make_fi(pcmobj, r_vdw)
    ui = 1 - fi
    ui[ui<0] = 0

    nlm = (lmax+1)**2
    Lmat = make_L(pcmobj, r_vdw, ylm_1sph, fi)
    Lmat = Lmat.reshape(natm*nlm,-1)

    def gen_vind(dm):
        v_phi = make_phi(pcmobj, dm, r_vdw, ui)
        phi = -numpy.einsum('n,xn,jn,jn->jx', weights_1sph, ylm_1sph,
                            ui, v_phi)
        L_X = numpy.linalg.solve(Lmat, phi.ravel()).reshape(natm,-1)
        psi, vmat = make_psi_vmat(pcmobj, dm, r_vdw, ylm_1sph,
                                  grids, L_X, Lmat, ui)
        dielectric = pcmobj.eps
        f_epsilon = (dielectric-1.)/dielectric
        epcm = .5 * f_epsilon * numpy.einsum('jx,jx', psi, L_X)
        return epcm, vmat
    return gen_vind


def make_ylm(r, lmax):
    # spherical harmonics, the standard computation method is
    #:import scipy.special
    #:cosphi = r[:,2]
    #:sinphi = (1-cosphi**2)**.5
    #:costheta = r[:,0] / sinphi
    #:sintheta = r[:,1] / sinphi
    #:varphi = numpy.arccos(cosphi)
    #:theta = numpy.arccos(costheta)
    #:if sintheta < 0:
    #:    theta = 2*numpy.pi - theta
    #:ngrid = r.shape[0]
    #:ylms = []
    #:for l in range(lmax+1):
    #:    ylm = numpy.empty((l*2+1,ngrid))
    #:    ylm[l] = scipy.special.sph_harm(0, l, theta, varphi).real
    #:    for m in range(1, l+1):
    #:        f1 = scipy.special.sph_harm(-m, l, theta, varphi)
    #:        f2 = scipy.special.sph_harm( m, l, theta, varphi)
    #:        # complex to real spherical functions
    #:        if m % 2 == 1:
    #:            ylm[l-m] = (-f1.imag - f2.imag) / numpy.sqrt(2)
    #:            ylm[l+m] = ( f1.real - f2.real) / numpy.sqrt(2)
    #:        else:
    #:            ylm[l-m] = (-f1.imag + f2.imag) / numpy.sqrt(2)
    #:            ylm[l+m] = ( f1.real + f2.real) / numpy.sqrt(2)
    #:    ylms.append(ylm)

# libcint has a fast cart2sph transformation for low angular moment
    ngrid = r.shape[0]
    xs = numpy.ones((lmax+1,ngrid))
    ys = numpy.ones((lmax+1,ngrid))
    zs = numpy.ones((lmax+1,ngrid))
    for i in range(1,lmax+1):
        xs[i] = xs[i-1] * r[:,0]
        ys[i] = ys[i-1] * r[:,1]
        zs[i] = zs[i-1] * r[:,2]
    ylms = []
    for l in range(lmax+1):
        nd = (l+1)*(l+2)//2
        c = numpy.empty((nd,ngrid))
        k = 0
        for lx in reversed(range(0, l+1)):
            for ly in reversed(range(0, l-lx+1)):
                lz = l - lx - ly
                c[k] = xs[lx] * ys[ly] * zs[lz]
                k += 1
        ylm = gto.cart2sph(l, c.T).T
# when call libcint, p functions are ordered as px,py,pz
# reorder px,py,pz to p(-1),p(0),p(1)
#        if l == 1:
#            ylm = ylm[[1,2,0]]
        ylms.append(ylm)
    return ylms

def make_multipoler(r, lmax):
    # Stand
    #:rad = lib.norm(r, axis=1)
    #:ylms = make_ylm(r/rad.reshape(-1,1), lmax)
    #:pol = [rad**l*y for l, y in enumerate(ylms)]
    pol = make_ylm(r, lmax)
    return pol

def regularize_xt(t, eta):
    xt = numpy.zeros_like(t)
    inner = t <= 1-eta
    on_shell = (1-eta < t) & (t < 1)
    xt[inner] = 1
    ti = t[on_shell]
# JCTC, 9, 3637
    xt[on_shell] = 1./eta**5 * (1-ti)**3 * (6*ti**2 + (15*eta-12)*ti
                                            + 10*eta**2 - 15*eta + 6)
# JCP, 139, 054111
#        xt[on_shell] = 1./eta**4 * (1-ti)**2 * (ti-1+2*eta)**2
    return xt

def make_grids_one_sphere(lebedev_order):
    ngrid_1sph = gen_grid.LEBEDEV_ORDER[lebedev_order]
    leb_grid = numpy.empty((ngrid_1sph,4))
    gen_grid.libdft.MakeAngularGrid(leb_grid.ctypes.data_as(ctypes.c_void_p),
                                    ctypes.c_int(ngrid_1sph))
    coords_1sph = leb_grid[:,:3]
    # Note the Lebedev angular grids are normalized to 1 in pyscf
    weights_1sph = 4*numpy.pi * leb_grid[:,3]
    return coords_1sph, weights_1sph

def make_L(pcmobj, r_vdw, ylm_1sph, fi):
    # See JCTC, 9, 3637, Eq (18)
    mol = pcmobj.mol
    natm = mol.natm
    lmax = pcmobj.lmax
    eta = pcmobj.eta
    nlm = (lmax+1)**2

    coords_1sph, weights_1sph = make_grids_one_sphere(pcmobj.lebedev_order)
    ngrid_1sph = weights_1sph.size
    atom_coords = mol.atom_coords()
    ylm_1sph = ylm_1sph.reshape(nlm,ngrid_1sph)

    L_diag = numpy.zeros((natm,nlm))
    p1 = 0
    for l in range(lmax+1):
        p0, p1 = p1, p1 + (l*2+1)
        L_diag[:,p0:p1] = 4*numpy.pi/(l*2+1)
    L_diag *= r_vdw.reshape(-1,1)
    Lmat = numpy.diag(L_diag.ravel()).reshape(natm,nlm,natm,nlm)

    for ja in range(natm):
        # scale the weight, precontract d_nj and w_n
        # see JCTC 9, 3637, Eq (16) - (18)
        part_weights = weights_1sph.copy()
        part_weights[fi[ja]>1] /= fi[ja,fi[ja]>1]
        for ka in atoms_with_vdw_overlap(ja, atom_coords, r_vdw):
            vjk = r_vdw[ja] * coords_1sph + atom_coords[ja] - atom_coords[ka]
            tjk = lib.norm(vjk, axis=1) / r_vdw[ka]
            wjk = regularize_xt(tjk, eta)
            wjk *= part_weights
            pol = make_multipoler(vjk, lmax)
            p1 = 0
            for l in range(lmax+1):
                fac = 4*numpy.pi/(l*2+1) / r_vdw[ka]**l
                p0, p1 = p1, p1 + (l*2+1)
                a = numpy.einsum('xn,n,mn->xm', ylm_1sph, wjk, pol[l])
                Lmat[ja,:,ka,p0:p1] += -fac * a
    return Lmat

def make_fi(pcmobj, r_vdw):
    coords_1sph, weights_1sph = make_grids_one_sphere(pcmobj.lebedev_order)
    mol = pcmobj.mol
    eta = pcmobj.eta
    natm = mol.natm
    atom_coords = mol.atom_coords()
    ngrid_1sph = coords_1sph.shape[0]
    fi = numpy.zeros((natm,ngrid_1sph))
    for ia in range(natm):
        for ja in atoms_with_vdw_overlap(ia, atom_coords, r_vdw):
            v = r_vdw[ia]*coords_1sph + atom_coords[ia] - atom_coords[ja]
            rv = lib.norm(v, axis=1)
            t = rv / r_vdw[ja]
            xt = regularize_xt(t, eta)
            fi[ia] += xt
    fi[fi < 1e-20] = 0
    return fi

def make_phi(pcmobj, dm, r_vdw, ui):
    mol = pcmobj.mol
    natm = mol.natm
    coords_1sph, weights_1sph = make_grids_one_sphere(pcmobj.lebedev_order)
    ngrid_1sph = coords_1sph.shape[0]

    tril_dm = lib.pack_tril(dm) * 2
    nao = dm.shape[0]
    diagidx = numpy.arange(nao)
    diagidx = diagidx*(diagidx+1)//2 + diagidx
    tril_dm[diagidx] *= .5

    atom_coords = mol.atom_coords()
    atom_charges = mol.atom_charges()

    extern_point_idx = ui > 0
    cav_coords = (atom_coords.reshape(natm,1,3)
                  + numpy.einsum('r,gx->rgx', r_vdw, coords_1sph))

    v_phi = numpy.empty((natm,ngrid_1sph))
    for ia in range(natm):
# Note (-) sign is not applied to atom_charges, because (-) is explicitly
# included in rhs and L matrix
        d_rs = atom_coords.reshape(-1,1,3) - cav_coords[ia]
        v_phi[ia] = numpy.einsum('z,zp->p', atom_charges, 1./lib.norm(d_rs,axis=2))

    max_memory = pcmobj.max_memory - lib.current_memory()[0]
    blksize = int(max(max_memory*1e6/8/nao**2, 400))

    cav_coords = cav_coords[extern_point_idx]
    v_phi_e = numpy.empty(cav_coords.shape[0])
    for i0, i1 in lib.prange(0, cav_coords.shape[0], blksize):
        fakemol = _make_fakemol(cav_coords[i0:i1])
        v_nj = df.incore.aux_e2(mol, fakemol, intor='int3c2e', aosym='s2ij')
        v_phi_e[i0:i1] = numpy.einsum('x,xk->k', tril_dm, v_nj)
    v_phi[extern_point_idx] -= v_phi_e
    return v_phi

def make_psi_vmat(pcmobj, dm, r_vdw, ylm_1sph,
                  grids, L_X, L, ui):
    mol = pcmobj.mol
    natm = mol.natm
    lmax = pcmobj.lmax
    nlm = (lmax+1)**2

    cached_pol = cache_fake_multipoler(grids, r_vdw, lmax)
    i1 = 0
    scaled_weights = numpy.zeros(grids.weights.size)
    for ia in range(natm):
        fak_pol, leak_idx = cached_pol[mol.atom_symbol(ia)]
        i0, i1 = i1, i1 + leak_idx.size
        becke_weights = grids.weights[i0:i1]
        p1 = 0
        for l in range(lmax+1):
            fac = 4*numpy.pi/(l*2+1)
            p0, p1 = p1, p1 + (l*2+1)
            eta_nj = fac * numpy.einsum('mn,m->n', fak_pol[l], L_X[ia,p0:p1])
            scaled_weights[i0:i1] += eta_nj * becke_weights

    ni = numint._NumInt()
    max_memory = pcmobj.max_memory - lib.current_memory()[0]
    make_rho, nset, nao = ni._gen_rho_evaluator(mol, dm, 1)
    shls_slice = (0, mol.nbas)
    ao_loc = mol.ao_loc_nr()
    den = numpy.empty(grids.weights.size)
    vmat = numpy.zeros((nao,nao))
    p1 = 0
    aow = None
    for ao, mask, weight, coords \
            in ni.block_loop(mol, grids, nao, 0, max_memory):
        p0, p1 = p1, p1 + weight.size
        den[p0:p1] = weight * make_rho(0, ao, mask, 'LDA')
        aow = numpy.ndarray(ao.shape, order='F', buffer=aow)
        aow = numpy.einsum('pi,p->pi', ao, scaled_weights[p0:p1], out=aow)
        vmat += numint._dot_ao_ao(mol, ao, aow, mask, shls_slice, ao_loc)
    ao = aow = scaled_weights = None

    nelec_leak = 0
    psi = numpy.empty((natm,nlm))
    i1 = 0
    for ia in range(natm):
        fak_pol, leak_idx = cached_pol[mol.atom_symbol(ia)]
        i0, i1 = i1, i1 + leak_idx.size
        nelec_leak += den[i0:i1][leak_idx].sum()
        p1 = 0
        for l in range(lmax+1):
            fac = 4*numpy.pi/(l*2+1)
            p0, p1 = p1, p1 + (l*2+1)
            psi[ia,p0:p1] = fac * numpy.einsum('n,mn->m', den[i0:i1], fak_pol[l])
    logger.debug(pcmobj, 'electron leak %f', nelec_leak)

    L_S = numpy.linalg.solve(L.reshape(natm*nlm,-1), psi.ravel()).reshape(natm,-1)
    coords_1sph, weights_1sph = make_grids_one_sphere(pcmobj.lebedev_order)
    # JCP, 141, 184108, Eq (39)
    xi_jn = numpy.einsum('n,jn,xn,jx->jn', weights_1sph, ui, ylm_1sph, L_S)
    extern_point_idx = ui > 0
    cav_coords = (mol.atom_coords().reshape(natm,1,3)
                  + numpy.einsum('r,gx->rgx', r_vdw, coords_1sph))
    cav_coords = cav_coords[extern_point_idx]
    xi_jn = xi_jn[extern_point_idx]

    max_memory = pcmobj.max_memory - lib.current_memory()[0]
    blksize = int(max(max_memory*1e6/8/nao**2, 400))

    vmat_tril = 0
    for i0, i1 in lib.prange(0, xi_jn.size, blksize):
        fakemol = _make_fakemol(cav_coords[i0:i1])
        v_nj = df.incore.aux_e2(mol, fakemol, intor='int3c2e', aosym='s2ij')
        vmat_tril += numpy.einsum('xn,n->x', v_nj, xi_jn[i0:i1])
    vmat += lib.unpack_tril(vmat_tril)
    return psi, vmat

def cache_fake_multipoler(grids, r_vdw, lmax):
# For each type of atoms, cache the product of last two terms in
# JCP, 141, 184108, Eq (31) x_{<}^{l} / x_{>}^{l+1} Y_l^m
    mol = grids.mol
    atom_grids_tab = grids.gen_atomic_grids(mol)
    r_vdw_type = {}
    for ia in range(mol.natm):
        symb = mol.atom_symbol(ia)
        if symb not in r_vdw_type:
            r_vdw_type[symb] = r_vdw[ia]

    cached_pol = {}
    for symb in atom_grids_tab:
        x_nj, w = atom_grids_tab[symb]
        r = lib.norm(x_nj, axis=1)
        leak_idx = r > r_vdw_type[symb]
        pol = make_multipoler(x_nj, lmax)
        fak_pol = []
        for l in range(lmax+1):
            # x_{<}^{l} / x_{>}^{l+1} Y_l^m  in JCP, 141, 184108, Eq (31)
            #:Ys = make_ylm(x_nj/r.reshape(-1,1), lmax)
            #:rr = numpy.zeros_like(r)
            #:rr[r<=r_vdw[ia]] = r[r<=r_vdw[ia]]**l / r_vdw[ia]**(l+1)
            #:rr[r> r_vdw[ia]] = r_vdw[ia]**l / r[r>r_vdw[ia]]**(l+1)
            #:xx_ylm = numpy.einsum('n,mn->mn', rr, Ys[l])
            xx_ylm = pol[l] * (1./r_vdw[ia]**(l+1))
            xx_ylm[:,leak_idx] *= (r_vdw[ia]/r[leak_idx])**(2*l+1)
            fak_pol.append(xx_ylm)
        cached_pol[symb] = (fak_pol, leak_idx)
    return cached_pol

def atoms_with_vdw_overlap(atm_id, atom_coords, r_vdw):
    atm_dist = atom_coords - atom_coords[atm_id]
    atm_dist = numpy.einsum('pi,pi->p', atm_dist, atm_dist)
    atm_dist[atm_id] = 1e200
    vdw_sum = r_vdw + r_vdw[atm_id]
    atoms_nearby = numpy.where(atm_dist < vdw_sum**2)[0]
    return atoms_nearby

class DDCOSMO(lib.StreamObject):
    def __init__(self, mol):
        self.mol = mol
        self.stdout = mol.stdout
        self.verbose = mol.verbose
        self.max_memory = mol.max_memory

        self.lebedev_order = 17
        self.lmax = 6  # max angular momentum of spherical harmonics basis
        self.eta = .1  # regularization parameter
        self.atomic_radii = elements.VDW_RADII
        self.eps = 78.3553
        self.becke_grids_level = 3

    def kernel(self, dm, grids=None):
        '''A single shot solvent effects for given density matrix.
        '''
        solver = self.as_solver(grids)
        e, vmat = solver(dm)
        return e, vmat

    def dump_flags(self):
        logger.info(self, '******** %s flags ********', self.__class__)
        logger.info(self, 'lebedev_order = %s', self.lebedev_order)
        logger.info(self, 'lmax = %s'         , self.lmax)
        logger.info(self, 'eta = %s'          , self.eta)
        logger.info(self, 'eps = %s'          , self.eps)
        return self

    gen_solver = as_solver = gen_ddcosmo_solver


def _make_fakemol(coords):
    nbas = coords.shape[0]
    fakeatm = numpy.zeros((nbas,gto.ATM_SLOTS), dtype=numpy.int32)
    fakebas = numpy.zeros((nbas,gto.BAS_SLOTS), dtype=numpy.int32)
    fakeenv = [0] * gto.PTR_ENV_START
    ptr = gto.PTR_ENV_START
    fakeatm[:,gto.PTR_COORD] = numpy.arange(ptr, ptr+nbas*3, 3)
    fakeenv.append(coords.ravel())
    ptr += nbas*3
    fakebas[:,gto.ATOM_OF] = numpy.arange(nbas)
    fakebas[:,gto.NPRIM_OF] = 1
    fakebas[:,gto.NCTR_OF] = 1
# approximate point charge with gaussian distribution exp(-1e14*r^2)
    fakebas[:,gto.PTR_EXP] = ptr
    fakebas[:,gto.PTR_COEFF] = ptr+1
    expnt = 1e14
    fakeenv.append([expnt, 1/(2*numpy.sqrt(numpy.pi)*gto.mole._gaussian_int(2,expnt))])
    ptr += 2
    fakemol = gto.Mole()
    fakemol._atm = fakeatm
    fakemol._bas = fakebas
    fakemol._env = numpy.hstack(fakeenv)
    fakemol._built = True
    return fakemol


if __name__ == '__main__':
    from pyscf import scf
    mol = gto.M(atom='H 0 0 0; H 0 1 1.2; H 1. .1 0; H .5 .5 1')
    natm = mol.natm
    r_vdw = [elements.VDW_RADII[gto.mole._charge(mol.atom_symbol(i))]
             for i in range(natm)]
    r_vdw = numpy.asarray(r_vdw)
    pcmobj = DDCOSMO(mol)
    pcmobj.lebedev_order = 7
    pcmobj.lmax = 6
    pcmobj.eta = 0.1
    nlm = (pcmobj.lmax+1)**2
    coords_1sph, weights_1sph = make_grids_one_sphere(pcmobj.lebedev_order)
    fi = make_fi(pcmobj, r_vdw)
    ylm_1sph = numpy.vstack(make_ylm(coords_1sph, pcmobj.lmax))
    L = make_L(pcmobj, r_vdw, ylm_1sph, fi)
    print(lib.finger(L) - 28.106385308171649)

    numpy.random.seed(1)
    nao = mol.nao_nr()
    dm = numpy.random.random((nao,nao))
    dm = dm + dm.T
    #dm = scf.RHF(mol).run().make_rdm1()
    e, vmat = DDCOSMO(mol).kernel(dm)
    print(e - 0.421535205256)
    print(lib.finger(vmat) - 0.098566328942023718)

    mol = gto.Mole()
    mol.atom = ''' O                  0.00000000    0.00000000   -0.11081188
                   H                 -0.00000000   -0.84695236    0.59109389
                   H                 -0.00000000    0.89830571    0.52404783 '''
    mol.basis = '3-21g' #cc-pvdz'
    mol.build()
    cm = DDCOSMO(mol)
    cm.verbose = 4
    mf = ddcosmo_for_scf(scf.RHF(mol), cm)#.newton()
    mf.verbose = 4
    mf.kernel()

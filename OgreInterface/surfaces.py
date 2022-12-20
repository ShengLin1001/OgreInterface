"""
This module will be used to construct the surfaces and interfaces used in this package.
"""
from pymatgen.core.structure import Structure 
from pymatgen.core.lattice import Lattice 
from pymatgen.core.surface import get_slab_regions, center_slab
from pymatgen.core.periodic_table import Element
from pymatgen.io.vasp.inputs import Poscar
from pymatgen.io.ase import AseAtomsAdaptor
from pymatgen.symmetry.analyzer import SpacegroupAnalyzer
from pymatgen.symmetry.structure import SymmetrizedStructure
from pymatgen.transformations.standard_transformations import PerturbStructureTransformation
from pymatgen.analysis.interfaces.zsl import ZSLGenerator, reduce_vectors
from pymatgen.analysis.interfaces.substrate_analyzer import SubstrateAnalyzer
from pymatgen.symmetry.analyzer import SymmOp
from pymatgen.analysis.molecule_structure_comparator import CovalentRadius
from pymatgen.analysis.ewald import EwaldSummation

from ase import Atoms
from ase.io import read
from ase.build.surfaces_with_termination import surfaces_with_termination
from ase.build import make_supercell, stack
from ase.spacegroup import get_spacegroup
from ase.geometry import get_layers
from ase.build.general_surface import ext_gcd
from ase.data.colors import jmol_colors
from ase.data import atomic_numbers
from ase.ga.utilities import closest_distances_generator, CellBounds
from ase.ga.startgenerator import StartGenerator
from ase.ga.data import PrepareDB

from OgreInterface.utils import group_layers
from OgreInterface.ewald import ionic_score_function
from OgreInterface.zbl import zbl_score_function
from OgreInterface.born import born_score_function

import matplotlib.pyplot as plt
from matplotlib.patches import Circle
from matplotlib.collections import PatchCollection
from matplotlib.colors import Normalize
from mpl_toolkits.axes_grid1 import make_axes_locatable

from scipy.stats import multivariate_normal
from scipy.signal import argrelextrema
from scipy.interpolate import RectBivariateSpline
from scipy.spatial.distance import cdist
from sklearn.cluster import AffinityPropagation, MeanShift

from itertools import combinations, product, repeat
from multiprocessing import Pool, cpu_count
from math import gcd
import numpy as np
import copy
import time


class Surface:
    """
    The Surface class is a container for surfaces generated with the SurfaceGenerator
    class and will be used as an input to the InterfaceGenerator class
    """

    def __init__(
        self,
        slab,
        bulk,
        miller_index,
        layers,
        vacuum,
    ):
        if type(slab) == Atoms:
            self.slab_ase = slab
            self.slab_pmg = AseAtomsAdaptor.get_structure(slab)
        elif type(slab) == Structure:
            self.slab_ase = AseAtomsAdaptor.get_atoms(slab)
            self.slab_pmg = slab
        else:
            raise TypeError(f"Structure accepts 'pymatgen.core.structure.Structure' or 'ase.Atoms' not '{type(slab).__name__}'")

        #  self.no_vac_slab_pmg = self._remove_vacuum(self.slab_pmg)
        #  self.no_vac_slab_ase = AseAtomsAdaptor.get_atoms(self.no_vac_slab_pmg)

        self.primitive_slab_pmg = self._get_primitive(self.slab_pmg)
        self.primitive_slab_ase = AseAtomsAdaptor.get_atoms(self._get_primitive(self.slab_pmg))

        if type(bulk) == Atoms:
            self.bulk_ase = bulk
            self.bulk_pmg = AseAtomsAdaptor.get_structure(bulk)
        elif type(bulk) == Structure:
            self.bulk_ase = AseAtomsAdaptor.get_atoms(bulk)
            self.bulk_pmg = bulk
        else:
            raise TypeError(f"structure accepts 'pymatgen.core.structure.Structure' or 'ase.Atoms' not '{type(bulk).__name__}'")

        self.miller_index = miller_index
        self.layers = layers
        self.vacuum = vacuum
        self.termination = self._get_termination()
        self.crystallographic_directions = self._get_crystallographic_directions()
        self.area = np.linalg.norm(np.cross(self.slab_pmg.lattice.matrix[0], self.slab_pmg.lattice.matrix[1]))

    def remove_layers(self, num_layers, top=False, atol=None):
        group_inds_conv, _ = group_layers(structure=self.slab_pmg, atol=atol)
        group_inds_prim, _ = group_layers(structure=self.primitive_slab_pmg, atol=atol)
        if top:
            group_inds_conv = group_inds_conv[::-1]
            group_inds_prim = group_inds_prim[::-1]
        
        to_delete_conv = []
        to_delete_prim = []
        for i in range(num_layers):
            to_delete_conv.extend(group_inds_conv[i])
            to_delete_prim.extend(group_inds_prim[i])

        self.slab_pmg.remove_sites(to_delete_conv)
        self.primitive_slab_pmg.remove_sites(to_delete_prim)
        # self.primitive_slab_pmg = self._get_primitive(self.slab_pmg)


    def _get_ewald_energy(self):
        slab = copy.deepcopy(self.slab_pmg)
        bulk = copy.deepcopy(self.bulk_pmg)
        slab.add_oxidation_state_by_guess()
        bulk.add_oxidation_state_by_guess()
        E_slab = EwaldSummation(slab).total_energy
        E_bulk = EwaldSummation(bulk).total_energy

        return E_slab, E_bulk

    def passivate(self, bot=True, top=True, passivated_struc=None):
        primitive_pas = passivator(
            struc=self.primitive_slab_pmg,
            bot=bot,
            top=top,
            symmetrize=False,
            passivated_struc=passivated_struc,
        )
        # surface = Surface(
        #     slab=slab,
        #     bulk=self.structure,
        #     miller_index=self.miller_index,
        #     layers=self.layers,
        #     vacuum=self.vacuum,
        # )
        pass

    def _get_primitive(self, structure):
        if type(structure) == Atoms:
            structure = AseAtomsAdaptor.get_structure(structure)

        spacegroup = SpacegroupAnalyzer(structure)
        primitive = spacegroup.get_primitive_standard_structure()

        return primitive.get_sorted_structure()

    def _remove_vacuum(self, slab):
        struc = copy.deepcopy(slab)
        bot, _ = get_slab_regions(struc)[0]
        struc.translate_sites(range(len(struc)), [0,0,-bot])
        frac_coords = struc.frac_coords
        cart_coords = struc.cart_coords
        max_z = np.max(frac_coords[:,-1])
        _, top_new = get_slab_regions(struc)[0]
        matrix = copy.deepcopy(struc.lattice.matrix)
        matrix[2,2] = top_new * matrix[2,2]
        new_lattice = Lattice(matrix)
        struc.lattice = new_lattice

        for i in range(len(struc)):
            struc.sites[i].frac_coords[-1] = struc.sites[i].frac_coords[-1] / max_z
            struc.sites[i].coords[-1] = cart_coords[i,-1]

        return struc

    def _get_termination(self):
        z_layers, _ = get_layers(self.slab_ase, (0, 0, 1))
        top_layer = [i for i, val in enumerate(z_layers == max(z_layers)) if val]
        termination = np.unique(
            [self.slab_ase.get_chemical_symbols()[a] for a in top_layer]
        )

        return termination

    def _get_crystallographic_directions(self, tol=1e-10):
        """
        TODO: Figure out how to find the crystallographic directions of the a and b
        lattice vectors in the primitive slab
        """
        indices = np.asarray(self.miller_index)

        if indices.shape != (3,) or not indices.any() or indices.dtype != int:
            raise ValueError('%s is an invalid surface type' % indices)

        h, k, l = indices
        h0, k0, l0 = (indices == 0)

        if h0 and k0 or h0 and l0 or k0 and l0:  # if two indices are zero
            if not h0:
                c1, c2, c3 = [(0, 1, 0), (0, 0, 1), (1, 0, 0)]
            if not k0:
                c1, c2, c3 = [(0, 0, 1), (1, 0, 0), (0, 1, 0)]
            if not l0:
                c1, c2, c3 = [(1, 0, 0), (0, 1, 0), (0, 0, 1)]
        else:
            p, q = ext_gcd(k, l)
            a1, a2, a3 = self.bulk_ase.cell

            # constants describing the dot product of basis c1 and c2:
            # dot(c1,c2) = k1+i*k2, i in Z
            k1 = np.dot(p * (k * a1 - h * a2) + q * (l * a1 - h * a3),
                        l * a2 - k * a3)
            k2 = np.dot(l * (k * a1 - h * a2) - k * (l * a1 - h * a3),
                        l * a2 - k * a3)

            if abs(k2) > tol:
                i = -int(round(k1 / k2))  # i corresponding to the optimal basis
                p, q = p + i * l, q - i * k

            a, b = ext_gcd(p * k + q * l, h)


            c1 = (p * k + q * l, -p * h, -q * h)
            c2 = np.array((0, l, -k)) // abs(gcd(l, k))
            c3 = (b, a * p, a * q)

        return np.array([c1, c2, c3])


class Interface:

    def __init__(
        self,
        substrate,
        film,
        film_transformation,
        substrate_transformation,
        strain,
        angle_diff,
        sub_strain_frac,
        interfacial_distance,
        vacuum,
        center=False,
    ):
        self.center = center
        self.substrate = substrate
        self.film = film
        self.film_transformation = film_transformation
        self.substrate_transformation = substrate_transformation
        self.strain = strain
        self.angle_diff = angle_diff
        self.sub_strain_frac = sub_strain_frac
        self.vacuum = vacuum
        self.substrate_supercell = self._prepare_slab(
            self.substrate.slab_pmg,
            self.substrate_transformation
        )
        self.film_supercell = self._prepare_slab(
            self.film.slab_pmg,
            self.film_transformation
        )
        self.interface_sl_vectors = self._get_interface_sl_vecs()
        self.interfacial_distance = interfacial_distance
        self.interface_height = None
        self.strained_sub = self._strain_and_orient_sub()
        self.strained_film = self._strain_and_orient_film()
        self.interface, self.sub_part, self.film_part = self._stack_interface()

    def write_file(self, output='POSCAR_interface'):
        Poscar(self.interface).write_file(output)


    def shift_film(self, shift, fractional=False, inplace=False):
        if fractional:
            frac_shift = np.array(shift)
        else:
            shift = np.array(shift)

            if shift[-1] + self.interfacial_distance < 0.5:
                raise ValueError(f"The film shift results in an interfacial distance of less than 0.5 Angstroms which is non-physical")
            
            frac_shift = self.interface.lattice.get_fractional_coords(shift)

        film_ind = np.where((self.interface.frac_coords[:,-1] > self.interface_height) & (self.interface.frac_coords[:,-1] < 0.99))[0]

        if inplace:
            self.interface.translate_sites(
                film_ind,
                frac_shift,
            )
            self.film_part.translate_sites(
                range(len(self.film_part)),
                frac_shift,
            )
            self.interface_height += frac_shift[-1] / 2
            self.interfacial_distance += shift[-1]

        else:
            shifted_interface = copy.copy(self.interface)
            shifted_interface.translate_sites(
                film_ind,
                frac_shift,
            )
            # self.interface_height += frac_shift[-1] / 2
            # self.interfacial_distance += shift[-1]

            return shifted_interface



    def _get_supercell(self, slab, matrix):
        new_slab = copy.copy(slab)
        initial_slab_matrix = new_slab.lattice.matrix
        initial_reduced_vecs = reduce_vectors(
            initial_slab_matrix[0],
            initial_slab_matrix[1],
        )
        initial_reduced_lattice = Lattice(
            np.vstack([
                initial_reduced_vecs,
                initial_slab_matrix[-1],
            ])
        )
        new_slab.lattice = initial_reduced_lattice

        new_slab.make_supercell(scaling_matrix=matrix)
        supercell_slab_matrix = new_slab.lattice.matrix
        supercell_reduced_vecs = reduce_vectors(
            supercell_slab_matrix[0],
            supercell_slab_matrix[1],
        )
        supercell_reduced_lattice = Lattice(
            np.vstack([
                supercell_reduced_vecs,
                supercell_slab_matrix[-1],
            ])
        )
        new_slab.lattice = supercell_reduced_lattice

        return new_slab

    def _prepare_slab(self, slab, matrix):
        initial_slab_matrix = slab.lattice.matrix
        initial_reduced_vecs = reduce_vectors(
            initial_slab_matrix[0],
            initial_slab_matrix[1],
        )
        initial_reduced_lattice = Lattice(np.vstack([
            initial_reduced_vecs,
            initial_slab_matrix[-1],
        ]))
        initial_reduced_slab = Structure(
            lattice=initial_reduced_lattice,
            species=slab.species,
            coords=slab.cart_coords,
            to_unit_cell=True,
            coords_are_cartesian=True,
        )
        supercell_slab = copy.copy(initial_reduced_slab)
        supercell_slab.make_supercell(scaling_matrix=matrix)
        supercell_slab_matrix = supercell_slab.lattice.matrix
        supercell_reduced_vecs = reduce_vectors(
            supercell_slab_matrix[0],
            supercell_slab_matrix[1],
        )
        supercell_reduced_lattice = Lattice(np.vstack([
            supercell_reduced_vecs,
            supercell_slab_matrix[-1],
        ]))
        initial_reduced_slab = Structure(
            lattice=supercell_reduced_lattice,
            species=supercell_slab.species,
            coords=supercell_slab.cart_coords,
            to_unit_cell=True,
            coords_are_cartesian=True,
        )

        return initial_reduced_slab


    def _get_angle(self, a, b):
        a_norm = np.linalg.norm(a)
        b_norm = np.linalg.norm(b)
        dot_prod = np.dot(a, b)
        angles = np.arccos(dot_prod / (a_norm * b_norm))

        return angles

    def _rotate(self, vec, angle):
        rot_mat = np.array([
            [np.cos(angle), -np.sin(angle), 0],
            [np.sin(angle), np.cos(angle), 0],
            [0, 0, 1],
        ])
        new_vec = np.matmul(rot_mat, vec.reshape(-1,1))

        return new_vec

    def _get_interface_sl_vecs(self):
        sub_sl_vecs = copy.copy(
            self.substrate_supercell.lattice.matrix[:2,:]
        )
        if self.sub_strain_frac == 0:
            new_sl_vecs = sub_sl_vecs

            return new_sl_vecs
        else:
            a_strain = self.strain[0]
            b_strain = self.strain[1]
            a_norm = np.linalg.norm(sub_sl_vecs[0])
            b_norm = np.linalg.norm(sub_sl_vecs[1])
            sub_angle = self._get_angle(sub_sl_vecs[0], sub_sl_vecs[1])
            new_angle = sub_angle * (1 + (self.sub_strain_frac * self.angle_diff))
            new_a = sub_sl_vecs[0] * (1 + (self.sub_strain_frac * a_strain))
            new_b = self._rotate(sub_sl_vecs[0] * (b_norm / a_norm), new_angle) * (1 + (self.sub_strain_frac * b_strain))
            new_sl_vecs = np.array([new_a, np.squeeze(new_b)])

            return new_sl_vecs

    def _get_vacuum_coords(self, structure):
        z_frac_coords = structure.frac_coords[:,-1]
        min_z = np.min(z_frac_coords)
        max_z = np.max(z_frac_coords)

        return min_z, max_z

    def _strain_and_orient_film(self):
        strained_film = copy.copy(self.film_supercell)
        new_lattice = Lattice(
            np.vstack([
                self.interface_sl_vectors,
                self.film_supercell.lattice.matrix[-1],
            ])
        )
        strained_film.lattice = new_lattice

        return strained_film

    def _strain_and_orient_sub(self):
        strained_sub = copy.copy(self.substrate_supercell)
        new_lattice = Lattice(
            np.vstack([
                self.interface_sl_vectors,
                self.substrate_supercell.lattice.matrix[-1],
            ])
        )
        strained_sub.lattice = new_lattice

        return strained_sub

    def _flip_structure(self, structure):
        copy_structure = copy.copy(structure)
        operation = SymmOp.from_origin_axis_angle(
            origin=[0.5,0.5,0.5],
            axis=[1,1,0],
            angle=180,
        )
        copy_structure.apply_operation(operation, fractional=True)

        return copy_structure

    def _get_unique_species(self):
        substrate_species = np.unique(self.substrate.bulk_pmg.species).astype(str)
        film_species = np.unique(self.film.bulk_pmg.species).astype(str)

        species_in_both = []
        for species in substrate_species:
            if species in film_species:
                species_in_both.append(species)

        return substrate_species, film_species, species_in_both

    def _stack_interface(self):
        strained_sub = self.strained_sub
        strained_film = self.strained_film
        strained_sub_coords = copy.deepcopy(strained_sub.frac_coords)
        strained_film_coords = copy.deepcopy(strained_film.frac_coords)

        min_sub_coords = np.min(strained_sub_coords[:,-1])
        max_sub_coords = np.max(strained_sub_coords[:,-1])
        min_film_coords = np.min(strained_film_coords[:,-1])
        max_film_coords = np.max(strained_film_coords[:,-1])

        sub_c_len = np.linalg.norm(strained_sub.lattice.matrix[-1])
        film_c_len = np.linalg.norm(strained_film.lattice.matrix[-1])
        interface_c_len = np.sum([
            (max_sub_coords - min_sub_coords) * sub_c_len,
            (max_film_coords - min_film_coords) * film_c_len,
            self.vacuum,
            self.interfacial_distance,
        ])

        sub_conv_factor = sub_c_len / interface_c_len
        film_conv_factor = film_c_len / interface_c_len

        strained_sub_coords[:,-1] *= sub_conv_factor
        strained_film_coords[:,-1] *= film_conv_factor

        sub_size = np.max(strained_sub_coords[:,-1]) - np.min(strained_sub_coords[:,-1])

        strained_sub_coords[:,-1] -= np.min(strained_sub_coords[:,-1])
        strained_film_coords[:,-1] -= np.min(strained_film_coords[:,-1])
        strained_film_coords[:,-1] += sub_size + (self.interfacial_distance / interface_c_len)

        interface_coords = np.r_[strained_sub_coords, strained_film_coords]
        interface_species = strained_sub.species + strained_film.species

        self.interface_height =  sub_size + ((0.5 * self.interfacial_distance) / interface_c_len)

        interface_lattice = Lattice(
            np.vstack([
                strained_sub.lattice.matrix[:2],
                (interface_c_len / np.linalg.norm(strained_sub.lattice.matrix[:,-1])) * strained_sub.lattice.matrix[:,-1] 
            ])
        )

        interface_struc = Structure(
            lattice=interface_lattice,
            species=interface_species,
            coords=interface_coords,
            to_unit_cell=True,
        )

        reduced_interface_struc = interface_struc.get_reduced_structure()
        sg = SpacegroupAnalyzer(reduced_interface_struc)
        refined_interface_struc = sg.get_conventional_standard_structure()
        primitive_interface_struc = refined_interface_struc.get_reduced_structure()
        primitive_interface_struc = primitive_interface_struc.get_primitive_structure()
        # print('REMINDER: line 498 surfaces.py')
        # primitive_interface_struc = refined_interface_struc

        substrate_species, film_species, species_in_both = self._get_unique_species()

        if len(species_in_both) == 0:
            pass
        else:
            for i in species_in_both:
                substrate_species = np.delete(substrate_species, np.where(substrate_species == i))
                film_species = np.delete(film_species, np.where(film_species == i))

        element_array_prim = np.array([
            site.species.elements[0].symbol for site in primitive_interface_struc
        ])

        substrate_ind_prim = np.isin(element_array_prim, substrate_species)
        film_ind_prim = np.isin(element_array_prim, film_species)

        average_sub_height_prim = np.mean(primitive_interface_struc.frac_coords[substrate_ind_prim,-1])
        average_film_height_prim = np.mean(primitive_interface_struc.frac_coords[film_ind_prim,-1])

        if average_film_height_prim < average_sub_height_prim:
            primitive_interface_struc = self._flip_structure(primitive_interface_struc)
        else:
            pass

        if self.center:
            primitive_interface_struc.translate_sites(
                    indices=range(len(primitive_interface_struc)),
                    vector=[0,0,0.5 - self.interface_height],
                    )
            self.interface_height = 0.5

        primitive_film = copy.deepcopy(primitive_interface_struc)
        primitive_sub = copy.deepcopy(primitive_interface_struc)

        film_sites = np.where(primitive_film.frac_coords[:,-1] > self.interface_height)[0]
        sub_sites = np.where(primitive_sub.frac_coords[:,-1] < self.interface_height)[0]

        primitive_film.remove_sites(sub_sites)
        primitive_sub.remove_sites(film_sites)

        return primitive_interface_struc , primitive_sub, primitive_film

    def _setup_for_surface_matching(self, layers_sub=2, layers_film=2):
        interface = self.interface
        interface_layer_inds, interface_layes_heights = group_layers(interface)
        dist_from_interface = interface_layes_heights - self.interface_height 
        sub_layer_inds = np.where(dist_from_interface < 0)[0]
        film_layer_inds = np.where(dist_from_interface > 0)[0]
        top_sub_inds = sub_layer_inds[-layers_sub:]
        bot_film_inds = film_layer_inds[:layers_film]
        
        sub_inds = np.concatenate([interface_layer_inds[i] for i in top_sub_inds])
        film_inds = np.concatenate([interface_layer_inds[i] for i in bot_film_inds])

        smaller_interface_lattice = np.copy(interface.lattice.matrix)
        interface_c = np.linalg.norm(interface.lattice.matrix[-1])

        sub_coords = interface.frac_coords[sub_inds]
        film_coords = interface.frac_coords[film_inds]
        min_val = np.min([sub_coords[:,-1].min(), film_coords[:,-1].min()])
        sub_coords[:,-1] -= min_val
        film_coords[:,-1] -= min_val

        sub_species = [interface.species[i] for i in sub_inds]
        film_species = [interface.species[i] for i in film_inds]

        smaller_interface_c = (np.max([sub_coords[:,-1].max(), film_coords[:,-1].max()]) * interface_c) + 4
        smaller_interface_lattice[-1] /= np.linalg.norm(smaller_interface_lattice[-1])
        smaller_interface_lattice[-1] *= smaller_interface_c
        conv_factor = interface_c / smaller_interface_c

        sub_coords[:,-1] *= conv_factor
        film_coords[:,-1] *= conv_factor

        max_val = np.max([sub_coords[:,-1].max(), film_coords[:,-1].max()])
        sub_coords[:,-1] += (1 - max_val) / 2
        film_coords[:,-1] += (1 - max_val) / 2

        sub_struc = Structure(
            lattice=Lattice(smaller_interface_lattice),
            species=sub_species,
            coords=sub_coords,
            to_unit_cell=True,
        )

        film_struc = Structure(
            lattice=Lattice(smaller_interface_lattice),
            species=film_species,
            coords=film_coords,
            to_unit_cell=True,
        )

        return sub_struc, film_struc

    @property
    def _metallic_elements(self):
        elements_list = np.array(
            ['Li', 'Be', 'Na', 'Mg', 'Al', 'K', 'Ca', 'Sc', 'Ti', 'V', 'Cr', 'Mn', 'Fe', 'Co', 'Ni', 'Cu', 'Zn', 'Ga', 'Rb', 'Sr', 'Y', 'Zr', 'Nb', 'Mo', 'Tc', 'Ru', 'Rh', 'Pd', 'Ag', 'Cd', 'In', 'Sn', 'Cs', 'Ba', 'La', 'Ce', 'Pr', 'Nd', 'Pm', 'Sm', 'Eu', 'Gd', 'Tb', 'Dy', 'Ho', 'Er', 'Tm', 'Yb', 'Lu', 'Hf', 'Ta', 'W', 'Re', 'Os', 'Ir', 'Pt', 'Au', 'Hg', 'Tl', 'Pb', 'Bi', 'Rn', 'Fr', 'Ra', 'Ac', 'Th', 'Pa', 'U', 'Np', 'Pu', 'Am', 'Cm', 'Bk', 'Cf', 'Es', 'Fm', 'Md', 'No', 'Lr', 'Rf', 'Db', 'Sg', 'Bh', 'Hs', 'Mt', 'Ds ', 'Rg ', 'Cn ', 'Nh', 'Fl', 'Mc', 'Lv']
        )
        return elements_list

    def _get_radii(self):
        sub_species = np.unique(
            np.array(self.substrate.bulk_pmg.species, dtype=str)
        )
        film_species = np.unique(
            np.array(self.film.bulk_pmg.species, dtype=str)
        )

        sub_elements = [Element(s) for s in sub_species]
        film_elements = [Element(f) for f in film_species]

        sub_metal = np.isin(sub_species, self._metallic_elements)
        film_metal = np.isin(film_species, self._metallic_elements)

        if sub_metal.all():
            sub_dict = {sub_species[i]: sub_elements[i].metallic_radius for i in range(len(sub_elements))}
        else:
            Xs = [e.X for e in sub_elements]
            X_diff = np.abs([c[0] - c[1] for c in combinations(Xs, 2)])
            if (X_diff >= 1.7).any():
                sub_dict = {sub_species[i]: sub_elements[i].average_ionic_radius for i in range(len(sub_elements))}
            else:
                sub_dict = {s: CovalentRadius.radius[s] for s in sub_species}

        if film_metal.all():
            film_dict = {film_species[i]: film_elements[i].metallic_radius for i in range(len(film_elements))}
        else:
            Xs = [e.X for e in film_elements]
            X_diff = np.abs([c[0] - c[1] for c in combinations(Xs, 2)])
            if (X_diff >= 1.7).any():
                film_dict = {film_species[i]: film_elements[i].average_ionic_radius for i in range(len(film_elements))}
            else:
                film_dict = {f: CovalentRadius.radius[f] for f in film_species}

        sub_dict.update(film_dict)

        return sub_dict

    def _get_intercept(self, midpoint, vector):
        if vector[0] == 0:
            intersect = [0, midpoint[1]]
        else:
            slope = vector[1] / vector[0]
            f = ((slope * midpoint[1]) + midpoint[0])/ ((slope**2) + 1)
            intersect = [f, slope * f]

        return intersect

    def _get_ratio(self, a, b):
        a_norm = np.linalg.norm(a)
        b_norm = np.linalg.norm(b)
        ratio_array = np.ones(2)
        min_ind = np.argmin([a_norm, b_norm])
        ratio = np.max([a_norm, b_norm]) / np.min([a_norm, b_norm])
        ratio_array[min_ind] = ratio
        
        return ratio_array

    def _get_square(self, a, b):
        midpoint = 0.5 * (a + b)
        a_inter = self._get_intercept(midpoint, a)
        b_inter = self._get_intercept(midpoint, b)
        a_len = np.linalg.norm(a_inter - midpoint)
        b_len = np.linalg.norm(b_inter - midpoint)

        r = np.min([a_len, b_len])

        box_length = (2 * r) / np.sqrt(2)

        return box_length, midpoint

    def _get_scaling_matrix(self, a, b, scan_size=40):
        final_box_length = 0
        final_midpoint = np.zeros(2)
        ratio = self._get_ratio(a, b)
        scaling_matrix = np.ones(2)
        i = 1
        while final_box_length <= scan_size:
            i += 1
            a_new = a * int(i * ratio[0])
            b_new = b * int(i * ratio[1])
            box_length, midpoint = self._get_square(a_new, b_new)
            final_box_length = box_length
            final_midpoint = midpoint
            scaling_matrix[0] = int(i * ratio[0])
            scaling_matrix[1] = int(i * ratio[1])

        return scaling_matrix.astype(int), midpoint

    def _generate_supercell(self, X, Y, Z, scaling_matrix=[8,8]):
        scale_x = np.ceil(scaling_matrix[0] / 2).astype(int)
        scale_y = np.ceil(scaling_matrix[1] / 2).astype(int)

        x_iter = range(-scale_x, scale_x)
        y_iter = range(-scale_y, scale_y)

        X_new = np.hstack([np.vstack([X for _ in x_iter]) + i for i in y_iter])
        Y_new = np.hstack([np.vstack([Y + i for i in x_iter]) for _ in y_iter])
        Z_new = np.hstack([np.vstack([Z for _ in x_iter]) for _ in y_iter])

        cart_coords = np.c_[
            X_new.ravel(),
            Y_new.ravel(),
            np.zeros(X_new.ravel().shape)
        ].dot(self.interface.lattice.matrix)

        X_cart = cart_coords[:,0].reshape(X_new.shape)
        Y_cart = cart_coords[:,1].reshape(Y_new.shape)

        return X_cart, Y_cart, Z_new

    def _crop_scan(self, X, Y, Z, scan_size):
        edge = (scan_size/2) + 1

        neg_x_val = X.ravel()[np.abs(X + edge).argmin()]
        pos_x_val = X.ravel()[np.abs(X - edge).argmin()]
        neg_y_val = Y.ravel()[np.abs(Y + edge).argmin()]
        pos_y_val = Y.ravel()[np.abs(Y - edge).argmin()]

        X_mask = np.logical_or((X <= neg_x_val), (X >= pos_x_val))
        Y_mask = np.logical_or((Y <= neg_x_val), (Y >= pos_x_val))
        total_mask = np.logical_or(X_mask, Y_mask)

        #  X[total_mask] = np.nan
        #  Y[total_mask] = np.nan
        Z[total_mask] = np.nan

    # def _pdf(self, x, v, m, s):
    #     return v*multivariate_normal.pdf(x, m, s**2)

    # def _generate_PES(self, x, y, sigmas, mus, scales):
    #     z = np.zeros(x.shape)
    #     for sigma, mu, scale in zip(sigmas, mus, scales):
    #         z += scale * (1 / (2 * np.pi * sigma**2)) * np.exp(-((x - mu[0])**2 + (y - mu[1])**2) / (2 * sigma**2))

    #     return z

    def _generate_PES(self, x, y, sigmas, mus, scales):
        x = np.expand_dims(x, 0)
        y = np.expand_dims(y, 0)
        e = np.exp(-((x - mus[:,0][:,None])**2 + (y - mus[:,1][:,None])**2) / (2 * sigmas[:,None]**2))
        z = scales[:,None] * (1 / (2 * np.pi * sigmas[:,None]**2)) * e
        # for sigma, mu, scale in zip(sigmas, mus, scales):
        #     z += scale * (1 / (2 * np.pi * sigma**2)) * np.exp(-((x - mu[0])**2 + (y - mu[1])**2) / (2 * sigma**2))

        # return z.sum(axis=0)
        return z


    def _get_score_function_params(
            self,
            si,
            fi, 
            r,
            sub_z_shift = 0,
            film_z_shift = 0,
    ):
        coords = self.interface.frac_coords
        matrix = self.interface.lattice.matrix

        x1 = coords[np.repeat(si, len(fi)), 0]
        x2 = coords[np.tile(fi, len(si)), 0]

        y1 = coords[np.repeat(si, len(fi)), 1]
        y2 = coords[np.tile(fi, len(si)), 1]

        z1 = coords[np.repeat(si, len(fi)), 2] + sub_z_shift
        z2 = coords[np.tile(fi, len(si)), 2] + film_z_shift

        r1 = r[np.repeat(si, len(fi))]
        r2 = r[np.tile(fi, len(si))]

        x_shift = x1 - x2
        y_shift = y1 - y2
        z_shift = z1 - z2

        x_shift[x_shift < 0] += 1
        x_shift[x_shift > 1] -= 1
        y_shift[y_shift < 0] += 1
        y_shift[y_shift > 1] -= 1

        r1p = np.concatenate([r1 for _ in range(9)])
        r2p = np.concatenate([r2 for _ in range(9)])
        x_shift = np.concatenate([
            x_shift-1,
            x_shift-1,
            x_shift-1,
            x_shift,
            x_shift,
            x_shift,
            x_shift+1,
            x_shift+1,
            x_shift+1 
        ])
        y_shift = np.concatenate([
            y_shift-1,
            y_shift,
            y_shift+1,
            y_shift-1,
            y_shift,
            y_shift+1,
            y_shift-1,
            y_shift,
            y_shift+1
        ])
        z_shift = np.concatenate([z_shift for _ in range(9)])

        frac_shifts = np.c_[x_shift, y_shift, z_shift]
        cart_shifts = frac_shifts.dot(matrix)
        overlap_r = np.sqrt((r1p + r2p)**2 - cart_shifts[:,-1]**2)

        mus = cart_shifts[:,:2]
        sigmas = overlap_r / 3
        scales = (4/3) * np.pi * overlap_r**3
        scales /= scales.max()

        return mus, sigmas, scales

    def _get_score_function_params_old(
            self,
            si,
            fi, 
            r,
            sub_z_shift = 0,
            film_z_shift = 0,
    ):
        coords = self.interface.frac_coords
        matrix = self.interface.lattice.matrix

        x1 = coords[np.repeat(si, len(fi)), 0]
        x2 = coords[np.tile(fi, len(si)), 0]

        y1 = coords[np.repeat(si, len(fi)), 1]
        y2 = coords[np.tile(fi, len(si)), 1]

        z1 = coords[np.repeat(si, len(fi)), 2] + sub_z_shift
        z2 = coords[np.tile(fi, len(si)), 2] + film_z_shift

        r1 = r[np.repeat(si, len(fi))]
        r2 = r[np.tile(fi, len(si))]

        x_shift = x1 - x2
        y_shift = y1 - y2
        z_shift = z1 - z2

        x_shift[x_shift < 0] += 1
        x_shift[x_shift > 1] -= 1
        y_shift[y_shift < 0] += 1
        y_shift[y_shift > 1] -= 1

        r1p = np.concatenate([r1 for _ in range(9)])
        r2p = np.concatenate([r2 for _ in range(9)])
        x_shift = np.concatenate([
            x_shift-1,
            x_shift-1,
            x_shift-1,
            x_shift,
            x_shift,
            x_shift,
            x_shift+1,
            x_shift+1,
            x_shift+1 
        ])
        y_shift = np.concatenate([
            y_shift-1,
            y_shift,
            y_shift+1,
            y_shift-1,
            y_shift,
            y_shift+1,
            y_shift-1,
            y_shift,
            y_shift+1
        ])
        z_shift = np.concatenate([z_shift for _ in range(9)])

        frac_shifts = np.c_[x_shift, y_shift, z_shift]
        cart_shifts = frac_shifts.dot(matrix)
        overlap_r = np.sqrt((r1p + r2p)**2 - cart_shifts[:,-1]**2)

        mus = cart_shifts[:,:2]
        sigmas = overlap_r / 3

        d = np.abs(cart_shifts[:,-1])
        volume = np.divide(
            np.pi * (r1p + r2p - d)**2 * (d**2 + (2*d*r1p) + (2*d*r2p) + (6*r1p*r2p) - (3*(r1p**2)) - (3*(r2p**2))),
            (12 * d),
            out=np.zeros_like(d),
            where=(d != 0)
        )
        max_pes = (1 / (2 * np.pi * sigmas**2))
        scales = volume / max_pes
        # scales = (4/3) * np.pi * overlap_r**3
        # scales /= scales.max()
        # x, y = plot_coords[:,0], plot_coords[:,1]

        # ns = self._pdf(x=x, y=y, sigmas=sigmas, mus=mus, scales=vol)

        # X_new, Y_new, Z_new = self._generate_supercell(X, Y, ns.reshape(X.shape), scaling_matrix) 

        # return X_new, Y_new, Z_new
        return mus, sigmas, scales

    def _gradient(self, x, y, sigmas, mus, scales):
        x = np.expand_dims(x, 0)
        y = np.expand_dims(y, 0)
        e = np.exp(-((x - mus[:,0][:,None])**2 + (y - mus[:,1][:,None])**2) / (2 * sigmas[:,None]**2))
        dx = - scales[:,None] * ((x - mus[:,0][:,None]) / (2 * np.pi * sigmas[:,None]**4)) * e
        dy = - scales[:,None] * ((y - mus[:,1][:,None]) / (2 * np.pi * sigmas[:,None]**4)) * e

        return dx.sum(axis=0), dy.sum(axis=0)

    def _init_gd_positions(self, mus, sigmas):
        angle = 15 * (np.pi / 180)
        xs, ys = [], []
        for mu, sigma in zip(mus, sigmas):
            long_side = np.cos(angle) * (1 * sigma)
            short_side = np.sin(angle) * (1 * sigma)
            xs.extend([
                mu[0] - long_side,
                mu[0] + short_side,
                mu[0] + long_side,
                mu[0] - short_side
            ])
            ys.extend([
                mu[1] + short_side,
                mu[1] + long_side,
                mu[1] - short_side,
                mu[1] - long_side,
            ])

        xs = np.array(xs)
        ys = np.array(ys)

        matrix = self.interface.lattice.matrix
        inv_matrix = self.interface.lattice.inv_matrix
        frac_xy = np.c_[xs, ys, np.zeros(xs.shape)].dot(inv_matrix)[:,:2]

        inds_in_cell = ((frac_xy >= 0) & (frac_xy <= 1)).all(axis=1)
        coords_in_cell = np.c_[frac_xy[inds_in_cell], np.zeros(inds_in_cell.sum())].dot(matrix)

        xs_in_cell = coords_in_cell[:,0]
        ys_in_cell = coords_in_cell[:,1]

        return xs_in_cell, ys_in_cell


    def adam(self, x, y, sigmas, mus, scales, beta1=0.9, beta2=0.999, eta=0.0075, epsilon=1e-7, iterations=2000):
        opt_x, opt_y = [np.copy(x)], [np.copy(y)]
        m_x, m_y = np.zeros(x.shape), np.zeros(y.shape)
        v_x, v_y = np.zeros(x.shape), np.zeros(y.shape)

        for i in range(iterations):
            # s = time.time()
            dx, dy = self._gradient(x=opt_x[i], y=opt_y[i], sigmas=sigmas, mus=mus, scales=scales)
            m_xi = beta1 * m_x + (1 - beta1) * dx
            m_yi = beta1 * m_y + (1 - beta1) * dy
            v_xi = beta2 * v_x + (1 - beta2) * dx**2
            v_yi = beta2 * v_y + (1 - beta2) * dy**2
            m_x = m_xi
            m_y = m_yi
            v_x = v_xi
            v_y = v_yi
            m_hat_x = m_xi / (1 - beta1)
            m_hat_y = m_yi / (1 - beta1)
            v_hat_x = v_xi / (1 - beta2)
            v_hat_y = v_yi / (1 - beta2)
            update_x = m_hat_x / (np.sqrt(v_hat_x) + epsilon)
            update_y = m_hat_y / (np.sqrt(v_hat_y) + epsilon)
            opt_x.append(opt_x[i] - eta * update_x)
            opt_y.append(opt_y[i] - eta * update_y)

        opt_x = np.vstack(opt_x)
        opt_y = np.vstack(opt_y)

        matrix = self.interface.lattice.matrix
        inv_matrix = self.interface.lattice.inv_matrix
        frac_coords = np.c_[opt_x.ravel(), opt_y.ravel(), np.zeros(len(opt_y.ravel()))].dot(inv_matrix)
        frac_x = frac_coords[:,0].reshape(opt_x.shape)
        frac_y = frac_coords[:,1].reshape(opt_y.shape)

        frac_x = np.hstack([
            frac_x-1,
            frac_x-1,
            frac_x-1,
            frac_x,
            frac_x,
            frac_x,
            frac_x+1,
            frac_x+1,
            frac_x+1 
        ])
        frac_y = np.hstack([
            frac_y-1,
            frac_y,
            frac_y+1,
            frac_y-1,
            frac_y,
            frac_y+1,
            frac_y-1,
            frac_y,
            frac_y+1
        ])

        cart_coords = np.c_[frac_x.ravel(), frac_y.ravel(), np.zeros(len(frac_x.ravel()))].dot(matrix)
        cart_x = cart_coords[:,0].reshape(frac_x.shape)
        cart_y = cart_coords[:,1].reshape(frac_y.shape)
        final_xs, final_ys= cart_x[-1], cart_y[-1]
        clustering = MeanShift(bandwidth=self._get_clustering_bandwidth(mus)).fit(np.c_[final_xs, final_ys])
        centers = clustering.cluster_centers_

        frac_centers = np.c_[centers, np.zeros(len(centers))].dot(inv_matrix)[:,:2]
        inds_in_cell = ((frac_centers.round(2) >= 0) & (frac_centers.round(2) < 1)).all(axis=1)
        centers_in_cell = np.c_[frac_centers[inds_in_cell], np.zeros(inds_in_cell.sum())].dot(matrix)[:,:2]

        return opt_x, opt_y, centers_in_cell

    def _get_clustering_bandwidth(self, mus):
        dist_mus = cdist(mus, mus)
        min_dist = np.min(dist_mus[dist_mus != 0])

        return min_dist / 3

    def overlap_film_sub_single(self, X, Y, sub_coords, film_coords, sub_r, film_r):
        d = np.sqrt(((film_coords[0] + X) - sub_coords[0])**2 + ((film_coords[1] + Y) - sub_coords[1])**2 + (film_coords[2] - sub_coords[2])**2)
        r1 = sub_r
        r2 = film_r
        d[d <= np.abs(r1 - r2)] = np.abs(r1 - r2)
        d[d - r1 - r2 > 0] = 0
        volume = np.divide(
            np.pi * (r1 + r2 - d)**2 * (d**2 + (2*d*r1) + (2*d*r2) + (6*r1*r2) - (3*(r1**2)) - (3*(r2**2))),
            (12 * d),
            out=np.zeros_like(d),
            where=(d != 0)
        )

        return volume


    def overlap_film_sub(self, X, Y, coords, rs, fi, si):
        inds = np.c_[np.repeat(si, len(fi)), np.tile(fi, len(si))]
        volume = []
        for i in inds:
            volume.append(self.overlap_film_sub_single(X, Y, sub_coords=coords[i[0]], film_coords=coords[i[1]], sub_r=rs[i[0]], film_r=rs[i[1]]))

        volume = np.array(volume)
        return volume

    # def overlap_film_sub(self, X, Y, coords, rs, fi, si):
    #     inds = np.c_[np.repeat(si, len(fi)), np.tile(fi, len(si))]
    #     volume = np.zeros(X.shape)
    #     for i in inds:
    #         volume += self.overlap_film_sub_single(X, Y, sub_coords=coords[i[0]], film_coords=coords[i[1]], sub_r=rs[i[0]], film_r=rs[i[1]])

    #     return volume

    def run_surface_matching_paper(
        self,
        scan_size,
        custom_radius_dict=None,
        grid_density_x=200,
        grid_density_y=200,
        fontsize=18,
        cmap='jet',
        output='PES.png',
        output2='cross_sec.png',
        atol=None,
    ):
        """
        This function runs a PES scan using the geometry based score function

        Parameters:
            x_range (list): The x-range to show in the PES scan in fractional coordinates
            y_range (list): The y-range to show in the PES scan in fractional coordinates
            z_range (list or None): The range to show in the PES scan in fractional coordinates
                given interface structure
            grid_density_x (int): Number of grid points to sample in the x-direction
            grid_density_y (int): Number of grid points to sample in the y-direction
            output (str): File name to save the image
        """
        if custom_radius_dict is None:
            radius_dict = self._get_radii()
        else:
            if type(custom_radius_dict) == dict:
                radius_dict = custom_radius_dict

        species = np.array(self.interface.species, dtype=str)
        r = np.array([radius_dict[i] for i in species])

        layer_inds, heights = group_layers(self.interface, atol=atol)
        bot_film_ind = np.min(np.where(heights > self.interface_height))
        top_sub_ind = np.max(np.where(heights < self.interface_height))
        second_film_ind = bot_film_ind + 1
        second_sub_ind = top_sub_ind - 1

        fi = layer_inds[bot_film_ind]
        fi2 = layer_inds[second_film_ind]
        # fi = np.concatenate([fi, fi2])

        si = layer_inds[top_sub_ind]
        si2 = layer_inds[second_sub_ind]
        # si = np.concatenate([si, si2])

        matrix = self.interface.lattice.matrix
        a = matrix[0,:2]
        b = matrix[1,:2]
        borders = np.vstack([np.zeros(2), a, a + b, b, np.zeros(2)])

        film_coords = self.interface.frac_coords[fi]
        periodic_shifts = np.array([ 
            [0,0,0],
            [1,1,0],
            [1,0,0],
            [0,1,0],
            [-1,0,0],
            [0,-1,0],
            [-1,1,0],
            [1,-1,0],
            [-1,-1,0],
            [2,0,0],
            [0,2,0],
            [-2,0,0],
            [0,-2,0],
            [2,1,0],
            [1,2,0],
            [-2,1,0],
            [1,-2,0],
            [2,-1,0],
            [-1,2,0],
            [-2,-1,0],
            [-1,-2,0],
            [-2,-2,0],
            [2,2,0],
            [2,-2,0],
            [-2,2,0],
        ])
        film_coords = np.vstack([film_coords + shift for shift in periodic_shifts]).dot(matrix)
        film_rs = np.tile(r[fi], 25)

        sub_coords = self.interface.frac_coords[si].dot(matrix)
        sub_rs = r[si]

        coords = np.r_[film_coords, sub_coords]
        rs = np.concatenate([film_rs, sub_rs])
        film_inds = np.arange(len(film_rs))
        sub_inds = np.arange(len(film_rs), len(film_rs) + len(sub_rs))

        X_frac, Y_frac = np.meshgrid(
            np.linspace(0, 1, grid_density_x),
            np.linspace(0, 1, grid_density_y),
        )

        cart_coords = np.c_[
            X_frac.ravel(),
            Y_frac.ravel(),
            np.zeros(len(Y_frac.ravel()))
        ].dot(matrix)
        X = cart_coords[:,0].reshape(X_frac.shape)
        Y = cart_coords[:,1].reshape(Y_frac.shape)
        Z = self.overlap_film_sub(X=X, Y=Y, coords=coords, rs=rs, fi=film_inds, si=sub_inds)
        Z = Z.sum(axis=0)
        atom_volume = (4/3) * np.pi * np.concatenate([r[si], r[fi]])**3
        O_bar = Z / atom_volume.sum()
        film_z_coords = self.interface.frac_coords[fi,-1]
        sub_z_coords = self.interface.frac_coords[si,-1]
        max_film_ind = film_z_coords.argmax()
        min_sub_ind = sub_z_coords.argmin()
        max_z = film_z_coords[max_film_ind]  
        min_z = sub_z_coords[min_sub_ind] 
        z_cart = np.abs(np.array([0,0,max_z - min_z]).dot(matrix))
        z_cart[-1] += r[si][min_sub_ind] + r[fi][max_film_ind]
        vol_mat = np.copy(matrix)
        vol_mat[-1] = z_cart
        V_cell = np.abs(np.linalg.det(vol_mat))
        V_E = V_cell - atom_volume.sum() + Z
        E_bar = V_E / V_cell

        c = 1
        # Z_plot = (1 + O_bar)**2 + (c * E_bar)
        Z_plot = (c * E_bar)
        # Z_plot = (1 + O_bar)**2 
        Z_plot -= Z_plot.min()
        Z_plot /= Z_plot.max()

        x_size = borders[:,0].max() - borders[:,0].min()
        y_size = borders[:,1].max() - borders[:,1].min()
        ratio = y_size / x_size

        if ratio < 1:
            fig_x_size = 4.5 * (1 / ratio)
            fig_y_size = 4.5
        else:
            fig_x_size = 4.5
            fig_y_size = 4.5 * ratio

        fig, ax = plt.subplots(figsize=(fig_x_size, fig_y_size), dpi=400)
        ax.set_xlabel(r"Shift in $x$ ($\AA$)", fontsize=fontsize)
        ax.set_ylabel(r"Shift in $y$ ($\AA$)", fontsize=fontsize)


        im = ax.pcolormesh(
            X,
            Y,
            Z_plot,
            cmap=cmap,
            shading='gouraud',
            norm=Normalize(vmin=np.nanmin(Z_plot), vmax=np.nanmax(Z_plot)),
        )

        ax.plot(
            borders[:,0],
            borders[:,1],
            color='black',
            linewidth=2,
        )

        divider = make_axes_locatable(ax)
        cax = divider.append_axes("top", size="5%", pad=0.1)

        cbar = fig.colorbar(im, cax=cax, orientation='horizontal')
        cbar.ax.tick_params(labelsize=fontsize)
        cbar.ax.locator_params(nbins=2)
        cbar.set_label('Score (Arb. Units)', fontsize=fontsize)
        cax.xaxis.set_ticks_position("top")
        cax.xaxis.set_label_position("top")
        ax.tick_params(labelsize=fontsize)

        ax.set_xlim(borders[:,0].min(), borders[:,0].max())
        ax.set_ylim(borders[:,1].min(), borders[:,1].max())
        ax.set_aspect('equal')

        fig.tight_layout()
        fig.savefig(output, bbox_inches='tight')

        return None, None

    def run_surface_matching_volume(
        self,
        scan_size,
        custom_radius_dict=None,
        grid_density_x=200,
        grid_density_y=200,
        fontsize=18,
        cmap='jet',
        output='PES.png',
        output2='cross_sec.png',
        atol=None,
    ):
        """
        This function runs a PES scan using the geometry based score function

        Parameters:
            x_range (list): The x-range to show in the PES scan in fractional coordinates
            y_range (list): The y-range to show in the PES scan in fractional coordinates
            z_range (list or None): The range to show in the PES scan in fractional coordinates
                given interface structure
            grid_density_x (int): Number of grid points to sample in the x-direction
            grid_density_y (int): Number of grid points to sample in the y-direction
            output (str): File name to save the image
        """
        if custom_radius_dict is None:
            radius_dict = self._get_radii()
        else:
            if type(custom_radius_dict) == dict:
                radius_dict = custom_radius_dict

        species = np.array(self.interface.species, dtype=str)
        r = np.array([radius_dict[i] for i in species])

        layer_inds, heights = group_layers(self.interface, atol=atol)
        bot_film_ind = np.min(np.where(heights > self.interface_height))
        top_sub_ind = np.max(np.where(heights < self.interface_height))
        second_film_ind = bot_film_ind + 1
        second_sub_ind = top_sub_ind - 1

        fi = layer_inds[bot_film_ind]
        fi2 = layer_inds[second_film_ind]
        # fi = np.concatenate([fi, fi2])

        si = layer_inds[top_sub_ind]
        si2 = layer_inds[second_sub_ind]
        # si = np.concatenate([si, si2])

        matrix = self.interface.lattice.matrix
        a = matrix[0,:2]
        b = matrix[1,:2]
        borders = np.vstack([np.zeros(2), a, a + b, b, np.zeros(2)])

        film_coords = self.interface.frac_coords[fi]
        periodic_shifts = np.array([ 
            [0,0,0],
            [1,1,0],
            [1,0,0],
            [0,1,0],
            [-1,0,0],
            [0,-1,0],
            [-1,1,0],
            [1,-1,0],
            [-1,-1,0],
            [2,0,0],
            [0,2,0],
            [-2,0,0],
            [0,-2,0],
            [2,1,0],
            [1,2,0],
            [-2,1,0],
            [1,-2,0],
            [2,-1,0],
            [-1,2,0],
            [-2,-1,0],
            [-1,-2,0],
            [-2,-2,0],
            [2,2,0],
            [2,-2,0],
            [-2,2,0],
        ])
        film_coords = np.vstack([film_coords + shift for shift in periodic_shifts]).dot(matrix)
        film_rs = np.tile(r[fi], 25)

        sub_coords = self.interface.frac_coords[si].dot(matrix)
        sub_rs = r[si]

        coords = np.r_[film_coords, sub_coords]
        rs = np.concatenate([film_rs, sub_rs])
        film_inds = np.arange(len(film_rs))
        sub_inds = np.arange(len(film_rs), len(film_rs) + len(sub_rs))

        X_frac, Y_frac = np.meshgrid(
            np.linspace(0, 1, grid_density_x),
            np.linspace(0, 1, grid_density_y),
        )

        cart_coords = np.c_[
            X_frac.ravel(),
            Y_frac.ravel(),
            np.zeros(len(Y_frac.ravel()))
        ].dot(matrix)
        X = cart_coords[:,0].reshape(X_frac.shape)
        Y = cart_coords[:,1].reshape(Y_frac.shape)
        Z = self.overlap_film_sub(X=X, Y=Y, coords=coords, rs=rs, fi=film_inds, si=sub_inds)
        Z_components = np.copy(Z)[:,0]
        # Z_components = Z_components[np.logical_not(np.isclose(Z_components, 0, atol=0.01).all(axis=1))]
        # Z_components = Z_components[np.argsort(np.argmax(Z_components, axis=1))]
        # selected_inds = [0, 4, 11, 16, 5, 10, 3, 13, 9, 17, 7, 12, 1, 6, 14, 18, 15, 8, 2]
        # Z_components = Z_components[selected_inds]
        Z = Z.sum(axis=0)
        # Z -= Z.min()
        # Z /= Z.max()

        x_size = borders[:,0].max() - borders[:,0].min()
        y_size = borders[:,1].max() - borders[:,1].min()
        ratio = y_size / x_size

        if ratio < 1:
            fig_x_size = 4.5 * (1 / ratio)
            fig_y_size = 4.5
        else:
            fig_x_size = 4.5
            fig_y_size = 4.5 * ratio

        # print(fig_x_size, fig_y_size)
        fig, ax = plt.subplots(figsize=(fig_x_size, fig_y_size), dpi=400)
        ax.set_xlabel(r"Shift in $x$ ($\AA$)", fontsize=fontsize)
        ax.set_ylabel(r"Shift in $y$ ($\AA$)", fontsize=fontsize)

        Z_plot = np.copy(Z)
        Z_plot -= Z_plot.min()
        Z_plot /= Z_plot.max()

        im = ax.pcolormesh(
            X,
            Y,
            Z_plot,
            cmap=cmap,
            shading='gouraud',
            norm=Normalize(vmin=np.nanmin(Z_plot), vmax=np.nanmax(Z_plot)),
        )

        fig2, ax2 = plt.subplots(figsize=(fig_x_size,fig_y_size), dpi=400, sharex=True)
        ax2.set_xlabel('Shift along $\\vec{a}$ ($\\AA$)', fontsize=fontsize)
        ax2.set_ylabel('Score (Arb. Units)', fontsize=fontsize)
        ax2.tick_params(labelsize=fontsize, left=False, labelleft=False)
        x0 = X[0]
        y0 = Y[0]
        line_x = np.linalg.norm(np.c_[x0, y0], axis=1)
        line_y = Z[0]


        ax2.plot(
            line_x,
            line_y,
            color='black',
        )
        for comp in Z_components:
            ax2.fill_between(
                line_x,
                comp,
                alpha=0.1,
                color='blue',
            )
            ax2.plot(
                line_x,
                comp,
                color='blue',
            )

        ax2.set_xlim(line_x.min(), line_x.max())
        ax2.set_ylim(0, None)
        fig2.tight_layout(pad=0.4)
        fig2.subplots_adjust(left=0.175, top=0.8655, right=0.95, bottom=0.092)
        fig2.savefig(output2)

        ax.plot(
            borders[:,0],
            borders[:,1],
            color='black',
            linewidth=2,
        )

        divider = make_axes_locatable(ax)
        cax = divider.append_axes("top", size="5%", pad=0.1)

        cbar = fig.colorbar(im, cax=cax, orientation='horizontal')
        cbar.ax.tick_params(labelsize=fontsize)
        cbar.ax.locator_params(nbins=2)
        cbar.set_label('Score (Arb. Units)', fontsize=fontsize)
        cax.xaxis.set_ticks_position("top")
        cax.xaxis.set_label_position("top")
        ax.tick_params(labelsize=fontsize)

        ax.set_xlim(borders[:,0].min(), borders[:,0].max())
        ax.set_ylim(borders[:,1].min(), borders[:,1].max())
        ax.set_aspect('equal')

        ax.annotate(
            '$d_{int}$ = ' + f'{np.round(self.interfacial_distance, 3)}',
            xy=(0.03,0.03),
            xycoords='axes fraction',
            fontsize=14,
        )

        fig.tight_layout()
        fig.savefig(output, bbox_inches='tight')

        return None, None


    def run_surface_matching(
        self,
        scan_size,
        custom_radius_dict=None,
        grid_density_x=200,
        grid_density_y=200,
        fontsize=18,
        cmap='jet',
        output='PES.png',
        output2='cross_sec.png',
        xlims=None,
        ylims=None,
        atol=None,
    ):
        """
        This function runs a PES scan using the geometry based score function

        Parameters:
            x_range (list): The x-range to show in the PES scan in fractional coordinates
            y_range (list): The y-range to show in the PES scan in fractional coordinates
            z_range (list or None): The range to show in the PES scan in fractional coordinates
                given interface structure
            grid_density_x (int): Number of grid points to sample in the x-direction
            grid_density_y (int): Number of grid points to sample in the y-direction
            output (str): File name to save the image
        """
        if custom_radius_dict is None:
            radius_dict = self._get_radii()
        else:
            if type(custom_radius_dict) == dict:
                radius_dict = custom_radius_dict

        species = np.array(self.interface.species, dtype=str)
        r = np.array([radius_dict[i] for i in species]) 

        layer_inds, heights = group_layers(self.interface, atol=atol)
        # print(heights)
        # print(self.interface_height)
        bot_film_ind = np.min(np.where(heights > self.interface_height))
        top_sub_ind = np.max(np.where(heights < self.interface_height))
        second_film_ind = bot_film_ind + 1
        second_sub_ind = top_sub_ind - 1

        fi = layer_inds[bot_film_ind]
        fi2 = layer_inds[second_film_ind]
        film_z_shift = heights[bot_film_ind] - heights[second_film_ind]
        film_dist = self.interface.lattice.get_cartesian_coords([0,0,np.abs(film_z_shift)])[-1]

        si = layer_inds[top_sub_ind]
        si2 = layer_inds[second_sub_ind]
        sub_z_shift = heights[top_sub_ind] - heights[second_sub_ind]
        sub_dist = self.interface.lattice.get_cartesian_coords([0,0,np.abs(sub_z_shift)])[-1]

        scaling_matrix, _ = self._get_scaling_matrix(
            a=self.interface.lattice.matrix[0, :2], 
            b=self.interface.lattice.matrix[1, :2], 
            scan_size=scan_size
        )

        if scaling_matrix[0] == 1:
            scaling_matrix[0] = 2
        if scaling_matrix[1] == 1:
            scaling_matrix[1] = 2

        X_frac, Y_frac = np.meshgrid(
            np.linspace(0, 1, grid_density_x),
            np.linspace(0, 1, grid_density_y),
        )
        matrix = self.interface.lattice.matrix
        a = matrix[0,:2]
        b = matrix[1,:2]
        borders = np.vstack([np.zeros(2), a, a + b, b, np.zeros(2)])

        cart_coords = np.c_[
            X_frac.ravel(),
            Y_frac.ravel(),
            np.zeros(len(Y_frac.ravel()))
        ].dot(matrix)
        X = cart_coords[:,0].reshape(X_frac.shape)
        Y = cart_coords[:,1].reshape(Y_frac.shape)

        mus_orig, sigmas_orig, scales_orig = self._get_score_function_params_old(
            si=si,
            fi=fi, 
            r=r,
        )

        mus_sub, sigmas_sub, scales_sub = self._get_score_function_params_old(
            si=si2,
            fi=fi, 
            r=r,
            sub_z_shift=sub_z_shift,
        )

        mus_film, sigmas_film, scales_film = self._get_score_function_params_old(
            si=si,
            fi=fi2, 
            r=r,
            film_z_shift=film_z_shift,
        )

        x_init_orig, y_init_orig = self._init_gd_positions(mus=mus_orig, sigmas=sigmas_orig)
        x_gd_orig, y_gd_orig, all_centers_orig = self.adam(
            x_init_orig,
            y_init_orig,
            mus=mus_orig,
            sigmas=sigmas_orig,
            scales=scales_orig
        )
        PES_values_orig = self._generate_PES(
            all_centers_orig[:,0],
            all_centers_orig[:,1],
            mus=mus_orig,
            sigmas=sigmas_orig,
            scales=scales_orig
        ).round(4).sum(axis=0)
        PES_values_sub = self._generate_PES(
            all_centers_orig[:,0],
            all_centers_orig[:,1],
            mus=mus_sub,
            sigmas=sigmas_sub,
            scales=scales_sub
        ).round(4).sum(axis=0)
        PES_values_film = self._generate_PES(
            all_centers_orig[:,0],
            all_centers_orig[:,1],
            mus=mus_film,
            sigmas=sigmas_film,
            scales=scales_film
        ).round(4).sum(axis=0)

        # PES_rank_values = PES_values_orig + np.exp(-np.abs(sub_z_shift)) * PES_values_sub + np.exp(-np.abs(film_z_shift)) * PES_values_film

        PES_rank_values_1 = np.round(PES_values_orig, 4)
        unique_PES_values_1 = np.unique(PES_rank_values_1)
        degenerate_min_inds = (PES_rank_values_1 == unique_PES_values_1[0])
        PES_values_orig = PES_values_orig[degenerate_min_inds]
        PES_values_sub = PES_values_sub[degenerate_min_inds]
        PES_values_film = PES_values_film[degenerate_min_inds]
        min_centers_orig = all_centers_orig[degenerate_min_inds]
        PES_rank_values_2 = np.round(PES_values_orig + PES_values_sub + PES_values_film, 4)
        unique_PES_values_2 = np.unique(PES_rank_values_2)
        unique_inds = [np.where(PES_rank_values_2 == u)[0] for u in unique_PES_values_2]
        unique_shift_inds = [u[np.argmin(np.linalg.norm(min_centers_orig[u], axis=1))] for u in unique_inds]
        # min_shift_inds = unique_shift_inds[np.argmin(PES_rank_values_2[unique_shift_inds])]
        min_shift_inds = unique_shift_inds[np.argmax(PES_rank_values_2[unique_shift_inds])]
        min_center_orig = min_centers_orig[min_shift_inds]

        Z_orig = self._generate_PES(
            x=X.ravel(),
            y=Y.ravel(),
            mus=mus_orig,
            sigmas=sigmas_orig,
            scales=scales_orig
        ).reshape((-1,)+X.shape)

        # Z_orig -= Z_orig.min()
        # Z_orig /= Z_orig.max()


        # x_init_sub, y_init_sub = self._init_gd_positions(mus=mus_sub, sigmas=sigmas_sub)
        # x_gd_sub, y_gd_sub, centers_sub = self.adam(
        #     x_init_sub,
        #     y_init_sub,
        #     mus=mus_sub,
        #     sigmas=sigmas_sub,
        #     scales=scales_sub
        # )

        Z_sub = self._generate_PES(
            x=X.ravel(),
            y=Y.ravel(),
            mus=mus_sub,
            sigmas=sigmas_sub,
            scales=scales_sub,
        ).reshape((-1,) + X.shape)

        # Z_sub -= Z_sub.min()
        # Z_sub /= Z_sub.max()


        # x_init_film, y_init_film = self._init_gd_positions(mus=mus_film, sigmas=sigmas_film)
        # x_gd_film, y_gd_film , centers_film = self.adam(
        #     x_init_film,
        #     y_init_film,
        #     mus=mus_film,
        #     sigmas=sigmas_film,
        #     scales=scales_film
        # )

        Z_film = self._generate_PES(
            x=X.ravel(),
            y=Y.ravel(),
            mus=mus_film,
            sigmas=sigmas_film,
            scales=scales_film,
        ).reshape((-1,) + X.shape)

        # Z_film -= Z_film.min()
        # Z_film /= Z_film.max()

        x_size = borders[:,0].max() - borders[:,0].min()
        y_size = borders[:,1].max() - borders[:,1].min()
        ratio = y_size / x_size

        if ratio < 1:
            fig_x_size = 4.5 * (1 / ratio)
            fig_y_size = 4.5
        else:
            fig_x_size = 4.5
            fig_y_size = 4.5 * ratio

        # print(fig_x_size, fig_y_size)
        fig, ax = plt.subplots(figsize=(fig_x_size, fig_y_size), dpi=400)

        Zs = [Z_orig.sum(axis=0), Z_film.sum(axis=0), Z_sub.sum(axis=0)]
        # x_gds = [x_gd_orig, x_gd_film, x_gd_sub]
        # y_gds = [y_gd_orig, y_gd_film, y_gd_sub]

        ax.set_xlabel(r"Shift in $x$ ($\AA$)", fontsize=fontsize)
        ax.set_ylabel(r"Shift in $y$ ($\AA$)", fontsize=fontsize)

        plot_ind = 0

        Z_plot = Zs[plot_ind]
        Z_plot -= Z_plot.min()
        Z_plot /= Z_plot.max()

        im = ax.pcolormesh(
            X,
            Y,
            Z_plot,
            cmap=cmap,
            shading='gouraud',
            norm=Normalize(vmin=np.nanmin(Z_plot), vmax=np.nanmax(Z_plot)),
        )

        fig2, ax2 = plt.subplots(figsize=(fig_x_size,fig_y_size), dpi=400)
        ax2.set_xlabel('Shift along $\\vec{a}$ ($\\AA$)', fontsize=fontsize)
        ax2.set_ylabel('Score (Arb. Units)', fontsize=fontsize)
        ax2.tick_params(labelsize=fontsize, left=False, labelleft=False)
        x0 = X[0]
        y0 = Y[0]
        line_x = np.linalg.norm(np.c_[x0, y0], axis=1)
        line_y = Z_orig.sum(axis=0)[0]
        Z_components = Z_orig[:,0]
        # Z_components = Z_components[np.logical_not(np.isclose(Z_components, 0, atol=0.01).all(axis=1))]
        # selected_inds = [7, 0, 12, 15, 3, 11, 8, 2, 13, 16, 9, 4, 6, 14, 1, 17, 5, 10, 18]
        # # Z_components = Z_components[np.argsort(np.argmax(Z_components, axis=1))]
        # Z_components = Z_components[selected_inds]

        ax2.plot(
            line_x,
            line_y,
            color='black',
        )

        for comp in Z_components:
            ax2.fill_between(
                line_x,
                comp,
                alpha=0.1,
                color='blue',
            )
            ax2.plot(
                line_x,
                comp,
                color='blue',
            )

        ax2.set_xlim(line_x.min(), line_x.max())
        ax2.set_ylim(0, None)
        fig2.tight_layout(pad=0.4)
        fig2.subplots_adjust(left=0.175, top=0.8655, right=0.95, bottom=0.092)
        fig2.savefig(output2)

        # for j in range(x_gd_orig.shape[1]):
        #     ax.plot(
        #         x_gd_orig[:,j],
        #         y_gd_orig[:,j],
        #         color='white',
        #     )
        ax.scatter(
            min_centers_orig[:,0],
            min_centers_orig[:,1],
            color='red',
            marker='o',
            s=50,
        )

        ax.scatter(
            min_center_orig[0],
            min_center_orig[1],
            color='white',
            s=100,
            marker='X'
        )

        ax.plot(
            borders[:,0],
            borders[:,1],
            color='black',
            linewidth=2,
        )

        from mpl_toolkits.axes_grid1 import make_axes_locatable
        divider = make_axes_locatable(ax)
        cax = divider.append_axes("top", size="5%", pad=0.1)

        cbar = fig.colorbar(im, cax=cax, orientation='horizontal')
        cbar.ax.tick_params(labelsize=fontsize)
        cbar.ax.locator_params(nbins=2)
        cbar.set_label('Score (Arb. Units)', fontsize=fontsize)
        cax.xaxis.set_ticks_position("top")
        cax.xaxis.set_label_position("top")
        ax.tick_params(labelsize=fontsize)

        ax.set_xlim(borders[:,0].min(), borders[:,0].max())
        ax.set_ylim(borders[:,1].min(), borders[:,1].max())
        ax.set_aspect('equal')

        ax.annotate(
            '$d_{int}$ = ' + f'{np.round(self.interfacial_distance, 3)}',
            xy=(0.03,0.03),
            xycoords='axes fraction',
            fontsize=14,
        )

        fig.tight_layout()
        fig.savefig(output, bbox_inches='tight')

        return min_center_orig


    def get_ewald_energy(self):
        interface = copy.deepcopy(self.interface)
        interface.add_oxidation_state_by_guess()

        film_inds = np.where(interface.frac_coords[:,-1] > self.interface_height)[0]
        sub_inds = np.where(interface.frac_coords[:,-1] < self.interface_height)[0]

        film = copy.deepcopy(interface)
        film.remove_sites(sub_inds)

        substrate = copy.deepcopy(interface)
        substrate.remove_sites(film_inds)

        interface_E = EwaldSummation(interface).total_energy
        film_E = EwaldSummation(film).total_energy
        substrate_E = EwaldSummation(substrate).total_energy

        return (film_E + substrate_E) - interface_E

    def get_dispersion(self, calc_type='d3'):
        # from ase.calculators.dftd3 import DFTD3
        from dftd4.ase import DFTD4

        interface = copy.deepcopy(self.interface)

        film_inds = np.where(interface.frac_coords[:,-1] > self.interface_height)[0]
        sub_inds = np.where(interface.frac_coords[:,-1] < self.interface_height)[0]

        film = copy.deepcopy(interface)
        film.remove_sites(sub_inds)

        substrate = copy.deepcopy(interface)
        substrate.remove_sites(film_inds)

        interface_atoms = AseAtomsAdaptor().get_atoms(interface)
        sub_atoms = AseAtomsAdaptor().get_atoms(substrate)
        film_atoms = AseAtomsAdaptor().get_atoms(film)


        interface_atoms.set_calculator(DFTD4(method='PBE'))
        sub_atoms.set_calculator(DFTD4(method='PBE'))
        film_atoms.set_calculator(DFTD4(method='PBE'))

        # interface_atoms.set_calculator(DFTD3(xc='PBE'))
        # sub_atoms.set_calculator(DFTD3(xc='PBE'))
        # film_atoms.set_calculator(DFTD3(xc='PBE'))

        interface_E = interface_atoms.get_potential_energy()
        substrate_E = sub_atoms.get_potential_energy()
        film_E = film_atoms.get_potential_energy()

        return (film_E + substrate_E) - interface_E


    def _ewald_sum(self, structure, shift, film_inds):
        structure.translate_sites(indices=film_inds, vector=shift)
        E = EwaldSummation(structure).total_energy
        structure.translate_sites(indices=film_inds, vector=-shift)

        return E


    def run_surface_matching_ewald(
        self,
        grid_density_x=15,
        grid_density_y=15,
        fontsize=16,
        cmap='jet',
        output='PES.png',
        shift=True,
    ):
        #TODO

        interface = copy.deepcopy(self.interface)
        interface.add_oxidation_state_by_guess()

        film_inds = np.where(interface.frac_coords[:,-1] > self.interface_height)[0]
        sub_inds = np.where(interface.frac_coords[:,-1] < self.interface_height)[0]

        film = copy.deepcopy(interface)
        film.remove_sites(sub_inds)

        sub = copy.deepcopy(interface)
        sub.remove_sites(film_inds)

        film_E = EwaldSummation(film).total_energy
        sub_E = EwaldSummation(sub).total_energy

        matrix = self.interface.lattice.matrix
        a = matrix[0,:2]
        b = matrix[1,:2]
        x_grid = np.linspace(0,1,grid_density_x)
        y_grid = np.linspace(0,1,grid_density_y)
        X, Y = np.meshgrid(
            x_grid,
            y_grid,
        )
        frac_shifts = np.c_[X.ravel(), Y.ravel(), 0 * Y.ravel()]
        cart_shifts = frac_shifts.dot(matrix)
        X_cart = cart_shifts[:,0].reshape(X.shape)
        Y_cart = cart_shifts[:,1].reshape(Y.shape)

        inputs = zip(repeat(interface), frac_shifts, repeat(film_inds))

        with Pool(cpu_count()) as p:
            Z = p.starmap(self._ewald_sum, inputs)

        Z = (film_E + sub_E) - np.array(Z).reshape(X.shape)

        Z_spline = RectBivariateSpline(y_grid, x_grid, Z)
        x_grid_interp = np.linspace(0,1,401)
        y_grid_interp = np.linspace(0,1,401)
        X_interp, Y_interp = np.meshgrid(x_grid_interp, y_grid_interp)
        Z_interp = Z_spline.ev(Y_interp, X_interp)
        cart_shifts_interp = np.c_[X_interp.ravel(), Y_interp.ravel(), 0 * Y_interp.ravel()].dot(matrix)
        X_cart_interp = cart_shifts_interp[:,0].reshape(X_interp.shape) 
        Y_cart_interp = cart_shifts_interp[:,1].reshape(Y_interp.shape) 

        borders = np.vstack([np.zeros(2), a, a + b, b, np.zeros(2)])
        x_size = borders[:,0].max() - borders[:,0].min()
        y_size = borders[:,1].max() - borders[:,1].min()
        ratio = y_size / x_size

        fig, ax = plt.subplots(figsize=(5,5*ratio), dpi=400)
        ax.set_xlabel(r"Shift in $x$ ($\AA$)", fontsize=fontsize)
        ax.set_ylabel(r"Shift in $y$ ($\AA$)", fontsize=fontsize)

        max_inds = np.where(Z_interp == Z_interp.max())

        x_opt = X_cart_interp[max_inds]
        y_opt = Y_cart_interp[max_inds]

        if shift:
            self.shift_film(shift=[x_opt[0], y_opt[0], 0], fractional=False, inplace=True)


        im = ax.pcolormesh(
            X_cart_interp,
            Y_cart_interp,
            Z_interp,
            cmap=cmap,
            shading='gouraud',
            norm=Normalize(vmin=np.nanmin(Z), vmax=np.nanmax(Z)),
        )

        ax.plot(
            borders[:,0],
            borders[:,1],
            color='black',
            linewidth=2,
        )

        ax.scatter(
            x_opt,
            y_opt,
            color='white',
            s=100,
            marker='X'
        )

        from mpl_toolkits.axes_grid1 import make_axes_locatable
        divider = make_axes_locatable(ax)
        cax = divider.append_axes("top", size="5%", pad=0.1)

        cbar = fig.colorbar(im, cax=cax, orientation='horizontal')
        cbar.ax.tick_params(labelsize=fontsize)
        cbar.ax.locator_params(nbins=2)
        cbar.set_label('$E_{adh}$ (eV)', fontsize=fontsize)
        cax.xaxis.set_ticks_position("top")
        cax.xaxis.set_label_position("top")
        ax.tick_params(labelsize=fontsize)

        ax.set_xlim(borders[:,0].min(), borders[:,0].max())
        ax.set_ylim(borders[:,1].min(), borders[:,1].max())
        ax.set_aspect('equal')

        fig.tight_layout()
        fig.savefig(output, bbox_inches='tight')
        plt.close(fig)

        return Z_interp.max()

    def _get_shifted_atoms(self, shift_inds, shift):
        if type(shift) != np.ndarray:
            shift = np.array(shift)

        frac_coords = self.interface.frac_coords
        shifts = np.zeros(frac_coords.shape)
        frac_coords[shift_inds] += shift
        numbers = list(self.interface.atomic_numbers)
        cell = self.interface.lattice.matrix

        atoms = Atoms(
            scaled_positions=frac_coords,
            cell=cell,
            numbers=numbers,
            pbc=True,
        )
        atoms.wrap()

        return atoms


    def run_surface_matching_pmg_zbl(
        self,
        grid_density_x=15,
        grid_density_y=15,
        fontsize=16,
        dpi=400,
        cmap='jet',
        output='PES.png',
        shift=True,
        charge_dict=None,
    ):
        interface = copy.deepcopy(self.interface)

        if charge_dict is None:
            charge_dict = interface.composition.oxi_state_guesses()[0]

        film_inds = np.where(interface.frac_coords[:,-1] > self.interface_height)[0]
        sub_inds = np.where(interface.frac_coords[:,-1] < self.interface_height)[0]

        film = copy.deepcopy(self.film_part)
        sub = copy.deepcopy(self.sub_part)

        interface.add_oxidation_state_by_guess()
        film.add_oxidation_state_by_guess()
        sub.add_oxidation_state_by_guess()

        film_zbl_E = zbl_score_function(
            atoms=[AseAtomsAdaptor.get_atoms(film)],
        )[0] 
        sub_zbl_E = zbl_score_function(
            atoms=[AseAtomsAdaptor.get_atoms(sub)],
        )[0] 

        film_ewald_E = EwaldSummation(film).total_energy
        sub_ewald_E = EwaldSummation(sub).total_energy

        matrix = self.interface.lattice.matrix
        a = matrix[0,:2]
        b = matrix[1,:2]
        x_grid = np.linspace(0,1,grid_density_x)
        y_grid = np.linspace(0,1,grid_density_y)
        X, Y = np.meshgrid(
            x_grid,
            y_grid,
        )
        frac_shifts = np.c_[X.ravel(), Y.ravel(), 0 * Y.ravel()]
        cart_shifts = frac_shifts.dot(matrix)
        X_cart = cart_shifts[:,0].reshape(X.shape)
        Y_cart = cart_shifts[:,1].reshape(Y.shape)

        inputs = zip(repeat(film_inds), frac_shifts)
        with Pool(cpu_count()) as p:
            atoms_list = p.starmap(self._get_shifted_atoms, inputs)

        ewald_inputs = zip(repeat(interface), frac_shifts, repeat(film_inds))
        with Pool(cpu_count()) as p:
            int_ewald_E = p.starmap(self._ewald_sum, ewald_inputs)

        int_zbl_E = zbl_score_function(atoms=atoms_list)

        Z_zbl = (film_zbl_E + sub_zbl_E) - int_zbl_E.reshape(X.shape)
        Z_ewald = (film_ewald_E + sub_ewald_E) - np.array(int_ewald_E).reshape(X.shape)
        Z = Z_zbl + Z_ewald

        Z_spline = RectBivariateSpline(y_grid, x_grid, Z)
        x_grid_interp = np.linspace(0,1,401)
        y_grid_interp = np.linspace(0,1,401)
        X_interp, Y_interp = np.meshgrid(x_grid_interp, y_grid_interp)
        Z_interp = Z_spline.ev(Y_interp, X_interp)
        Z_interp -= Z_interp.min()
        cart_shifts_interp = np.c_[X_interp.ravel(), Y_interp.ravel(), 0 * Y_interp.ravel()].dot(matrix)
        X_cart_interp = cart_shifts_interp[:,0].reshape(X_interp.shape) 
        Y_cart_interp = cart_shifts_interp[:,1].reshape(Y_interp.shape) 

        borders = np.vstack([np.zeros(2), a, a + b, b, np.zeros(2)])
        x_size = borders[:,0].max() - borders[:,0].min()
        y_size = borders[:,1].max() - borders[:,1].min()
        ratio = y_size / x_size

        fig, ax = plt.subplots(figsize=(5,5*ratio), dpi=dpi)
        ax.set_xlabel(r"Shift in $x$ ($\AA$)", fontsize=fontsize)
        ax.set_ylabel(r"Shift in $y$ ($\AA$)", fontsize=fontsize)

        max_inds = np.where(Z_interp == Z_interp.max())

        x_opt = X_cart_interp[max_inds]
        y_opt = Y_cart_interp[max_inds]

        if shift:
            self.shift_film(shift=[x_opt[0], y_opt[0], 0], fractional=False, inplace=True)

        im = ax.contourf(
            X_cart_interp,
            Y_cart_interp,
            Z_interp,
            cmap=cmap,
            levels=200,
            # shading='gouraud',
            norm=Normalize(vmin=np.nanmin(Z_interp), vmax=np.nanmax(Z_interp)),
        )

        ax.plot(
            borders[:,0],
            borders[:,1],
            color='black',
            linewidth=2,
        )

        ax.scatter(
            x_opt,
            y_opt,
            fc='white',
            ec='black',
            s=100,
            marker='X'
        )

        from mpl_toolkits.axes_grid1 import make_axes_locatable
        divider = make_axes_locatable(ax)
        cax = divider.append_axes("top", size="5%", pad=0.1)

        cbar = fig.colorbar(im, cax=cax, orientation='horizontal')
        cbar.ax.tick_params(labelsize=fontsize)
        cbar.ax.locator_params(nbins=2)
        cbar.set_label('$E_{adh}$ (eV)', fontsize=fontsize)
        cax.xaxis.set_ticks_position("top")
        cax.xaxis.set_label_position("top")
        ax.tick_params(labelsize=fontsize)

        ax.set_xlim(borders[:,0].min(), borders[:,0].max())
        ax.set_ylim(borders[:,1].min(), borders[:,1].max())
        ax.set_aspect('equal')

        fig.tight_layout()
        fig.savefig(output, bbox_inches='tight')
        # plt.close()

        return Z_interp.max()

    def run_surface_matching_ionic(
        self,
        grid_density_x=15,
        grid_density_y=15,
        fontsize=16,
        dpi=400,
        cmap='jet',
        output='PES.png',
        shift=True,
        charge_dict=None,
        radius_dict=None,
    ):
        interface = copy.deepcopy(self.interface)

        if charge_dict is None:
            charge_dict = interface.composition.oxi_state_guesses()[0]

        film_inds = np.where(interface.frac_coords[:,-1] > self.interface_height)[0]
        sub_inds = np.where(interface.frac_coords[:,-1] < self.interface_height)[0]

        film = copy.deepcopy(self.film_part)
        sub = copy.deepcopy(self.sub_part)

        film_E, film_ewald_E, film_zbl_E = ionic_score_function(
            atoms=[AseAtomsAdaptor.get_atoms(film)],
            charge_dict=charge_dict,
        ) 
        sub_E, sub_ewald_E, sub_zbl_E = ionic_score_function(
            atoms=[AseAtomsAdaptor.get_atoms(sub)],
            charge_dict=charge_dict,
        ) 

        film_born_E = born_score_function(
            atoms=[AseAtomsAdaptor.get_atoms(film)],
            charge_dict=charge_dict,
            radius_dict=radius_dict,
            cutoff=36.0,
        )
        sub_born_E = born_score_function(
            atoms=[AseAtomsAdaptor.get_atoms(sub)],
            charge_dict=charge_dict,
            radius_dict=radius_dict,
            cutoff=36.0,
        )

        matrix = self.interface.lattice.matrix
        a = matrix[0,:2]
        b = matrix[1,:2]
        x_grid = np.linspace(0,1,grid_density_x)
        y_grid = np.linspace(0,1,grid_density_y)
        X, Y = np.meshgrid(
            x_grid,
            y_grid,
        )
        frac_shifts = np.c_[X.ravel(), Y.ravel(), 0 * Y.ravel()]
        cart_shifts = frac_shifts.dot(matrix)
        X_cart = cart_shifts[:,0].reshape(X.shape)
        Y_cart = cart_shifts[:,1].reshape(Y.shape)

        inputs = zip(repeat(film_inds), frac_shifts)
        with Pool(cpu_count()) as p:
            atoms_list = p.starmap(self._get_shifted_atoms, inputs)

        int_E, int_ewald_E, int_zbl_E = ionic_score_function(
            atoms=atoms_list,
            charge_dict=charge_dict,
            alpha=0.02,
            cutoff=36.0,
            k_max=20,
        )

        int_born_E = born_score_function(
            atoms=atoms_list,
            charge_dict=charge_dict,
            radius_dict=radius_dict,
            cutoff=36.0,
        )

        # Z = (film_E[0] + sub_E[0]) - np.array(int_E).reshape(X.shape)
        Z_ew = (film_ewald_E[0] + sub_ewald_E[0]) - np.array(int_ewald_E).reshape(X.shape)
        # Z_zb = (film_zbl_E[0] + sub_zbl_E[0]) - np.array(int_zbl_E).reshape(X.shape)
        Z_born = (film_born_E[0] + sub_born_E[0]) - np.array(int_born_E).reshape(X.shape)
        Z = Z_ew + Z_born
        # Z = np.array(int_E).reshape(X.shape)

        Z_spline = RectBivariateSpline(y_grid, x_grid, Z)
        x_grid_interp = np.linspace(0,1,401)
        y_grid_interp = np.linspace(0,1,401)
        X_interp, Y_interp = np.meshgrid(x_grid_interp, y_grid_interp)
        Z_interp = Z_spline.ev(Y_interp, X_interp)
        Z_interp -= Z_interp.min()
        cart_shifts_interp = np.c_[X_interp.ravel(), Y_interp.ravel(), 0 * Y_interp.ravel()].dot(matrix)
        X_cart_interp = cart_shifts_interp[:,0].reshape(X_interp.shape) 
        Y_cart_interp = cart_shifts_interp[:,1].reshape(Y_interp.shape) 

        borders = np.vstack([np.zeros(2), a, a + b, b, np.zeros(2)])
        x_size = borders[:,0].max() - borders[:,0].min()
        y_size = borders[:,1].max() - borders[:,1].min()
        ratio = y_size / x_size

        fig, ax = plt.subplots(figsize=(5,5*ratio), dpi=dpi)
        ax.set_xlabel(r"Shift in $x$ ($\AA$)", fontsize=fontsize)
        ax.set_ylabel(r"Shift in $y$ ($\AA$)", fontsize=fontsize)

        max_inds = np.where(Z_interp == Z_interp.max())

        x_opt = X_cart_interp[max_inds]
        y_opt = Y_cart_interp[max_inds]

        if shift:
            self.shift_film(shift=[x_opt[0], y_opt[0], 0], fractional=False, inplace=True)

        im = ax.contourf(
            X_cart_interp,
            Y_cart_interp,
            Z_interp,
            cmap=cmap,
            levels=200,
            # shading='gouraud',
            norm=Normalize(vmin=np.nanmin(Z_interp), vmax=np.nanmax(Z_interp)),
        )

        ax.plot(
            borders[:,0],
            borders[:,1],
            color='black',
            linewidth=2,
        )

        ax.scatter(
            x_opt,
            y_opt,
            fc='white',
            ec='black',
            s=100,
            marker='X'
        )

        from mpl_toolkits.axes_grid1 import make_axes_locatable
        divider = make_axes_locatable(ax)
        cax = divider.append_axes("top", size="5%", pad=0.1)

        cbar = fig.colorbar(im, cax=cax, orientation='horizontal')
        cbar.ax.tick_params(labelsize=fontsize)
        cbar.ax.locator_params(nbins=2)
        cbar.set_label('$E_{adh}$ (eV)', fontsize=fontsize)
        cax.xaxis.set_ticks_position("top")
        cax.xaxis.set_label_position("top")
        ax.tick_params(labelsize=fontsize)

        ax.set_xlim(borders[:,0].min(), borders[:,0].max())
        ax.set_ylim(borders[:,1].min(), borders[:,1].max())
        ax.set_aspect('equal')

        fig.tight_layout()
        fig.savefig(output, bbox_inches='tight')
        # plt.close()

        return Z_interp.max()

    def plot_interface(self, output='interface_view.png', layers_from_interface=[2,2], alpha=0.5, legend_fontsize=16, transparent=False, atol=None):
        layer_inds, heights = group_layers(self.interface, atol=atol)
        bot_film_ind = np.min(np.where(heights > self.interface_height))
        top_sub_ind = np.max(np.where(heights < self.interface_height))
        film_layer_inds = [bot_film_ind + i for i in range(layers_from_interface[1])]
        sub_layer_inds = [top_sub_ind - i for i in range(layers_from_interface[0])]
        interface_layer_inds = np.sort(film_layer_inds + sub_layer_inds)
        interface_atom_inds = [layer_inds[i] for i in interface_layer_inds]
        frac_coords = self.interface.frac_coords

        matrix = self.interface.lattice.matrix
        a = matrix[0,:2]
        b = matrix[1,:2]

        borders = np.vstack([np.zeros(2), a, a + b, b, np.zeros(2)])
        x_size = borders[:,0].max() - borders[:,0].min()
        y_size = borders[:,1].max() - borders[:,1].min()
        ratio = y_size / x_size

        fig, ax = plt.subplots(figsize=(4,4*ratio), dpi=400)

        supercell_shifts = np.array([ 
            [0,0,0],
            [-1,-1,0],
            [-1,0,0],
            [0,-1,0],
            [-1,1,0],
            [1,-1,0],
            [0,1,0],
            [1,0,0],
            [1,1,0],
        ])

        vesta_data = np.loadtxt('./vesta_colors.csv', delimiter=',')
        vesta_colors = np.c_[vesta_data[:,:3], alpha * np.ones(len(vesta_data))]
        vesta_radii = vesta_data[:,-1]

        all_unique_species = []

        for inds in interface_atom_inds:
            layer_atom_coords = frac_coords[inds]
            layer_atom_coords = (layer_atom_coords - supercell_shifts[:,None]).reshape(-1,3)
            inds_in_cell = ((layer_atom_coords[:,:2].round(2) >= 0) & (layer_atom_coords[:,:2].round(2) <= 1)).all(axis=1)
            layer_atom_coords = layer_atom_coords[inds_in_cell].dot(matrix)
            layer_atom_symbols = np.array(self.interface.species, dtype='str')[inds]
            layer_atom_symbols = np.concatenate([layer_atom_symbols for _ in range(9)])[inds_in_cell]
            layer_atom_species = np.zeros(layer_atom_symbols.shape, dtype=int)
            layer_atom_sizes = np.zeros(layer_atom_symbols.shape, dtype=float)
            unique_species = np.unique(layer_atom_symbols)
            unique_elements = [Element(i) for i in unique_species]


            for i, z in enumerate(unique_elements):
                layer_atom_species[np.isin(layer_atom_symbols, unique_species[i])] = z.Z
                layer_atom_sizes[np.isin(layer_atom_symbols, unique_species[i])] = z.atomic_radius


            all_unique_species.append(np.unique(layer_atom_species))
            colors = vesta_colors[layer_atom_species]
            layer_atom_sizes = vesta_radii[layer_atom_species] * 0.4

            for xy, r, c in zip(layer_atom_coords, layer_atom_sizes, colors):
                ax.add_patch(Circle(xy, radius=r, ec=c[:3], fc=c, linewidth=1.5, clip_on=False))

        ax.plot(
            borders[:,0],
            borders[:,1],
            color='black',
            linewidth=2,
            solid_capstyle='round'
        )
        ax.set_aspect('equal')
        ax.axis('off')

        all_unique_species = np.unique(np.concatenate(all_unique_species))

        legend_lines = []
        legend_labels = []
        for z in all_unique_species:
            legend_lines.append(plt.scatter(
                [np.nan],
                [np.nan],
                color=vesta_colors[z],
                s=50*vesta_radii[z]**2,
                ec=vesta_colors[z,:3],
            ))
            legend_labels.append(
                f'{Element.from_Z(z)}'
            )

        from mpl_toolkits.axes_grid1 import make_axes_locatable
        divider = make_axes_locatable(ax)
        lax = divider.append_axes("bottom", size="8%", pad=0.05)
        lax.axis('off')

        l = lax.legend(
            legend_lines,
            legend_labels,
            ncol=len(all_unique_species),
            loc='center',
            framealpha=1,
            fontsize=legend_fontsize,
            handletextpad=0,
            columnspacing=0.5,
            mode='expand',
        )
        l.set_zorder(200)
        frame = l.get_frame()
        frame.set_facecolor('white')
        frame.set_edgecolor('black')
        frame.set_linewidth(2)

        fig.tight_layout(pad=0.4)
        fig.savefig(output, transparent=transparent)

    def plot_interface_side(self, output='interface_view.png', layers_from_interface=[2,2], alpha=0.5, legend_fontsize=16, transparent=False, atol=None):
        layer_inds, heights = group_layers(self.interface, atol=atol)
        bot_film_ind = np.min(np.where(heights > self.interface_height))
        top_sub_ind = np.max(np.where(heights < self.interface_height))
        film_layer_inds = [bot_film_ind + i for i in range(layers_from_interface[1])]
        sub_layer_inds = [top_sub_ind - i for i in range(layers_from_interface[0])]
        interface_layer_inds = np.sort(film_layer_inds + sub_layer_inds)
        interface_atom_inds = [layer_inds[i] for i in interface_layer_inds]
        frac_coords = self.interface.frac_coords

        matrix = self.interface.lattice.matrix
        a = matrix[0,:2]
        b = matrix[1,:2]

        theta = np.arccos(np.dot(a,[0,1]) / np.linalg.norm(a))
        # rot_mat = np.array([ 
        #     [np.cos(theta), -np.sin(theta)],
        #     [np.sin(theta), np.cos(theta)],
        # ])
        rot_mat = np.array([
            [np.cos(theta), -np.sin(theta), 0],
            [np.sin(theta), np.cos(theta), 0],
            [0, 0, 1],
        ])

        borders = np.c_[a, b, np.zeros(0)].dot(rot_mat)
        x_size = borders[:,0].max() - borders[:,0].min()
        y_size = borders[:,1].max() - borders[:,1].min()
        ratio = y_size / x_size

        fig, ax = plt.subplots(figsize=(4,4*ratio), dpi=400)

        supercell_shifts = np.array([ 
            [0,0,0],
            [-1,-1,0],
            [-1,0,0],
            [0,-1,0],
            [-1,1,0],
            [1,-1,0],
            [0,1,0],
            [1,0,0],
            [1,1,0],
        ])

        vesta_data = np.loadtxt('./vesta_colors.csv', delimiter=',')
        vesta_colors = np.c_[vesta_data[:,:3], alpha * np.ones(len(vesta_data))]
        vesta_radii = vesta_data[:,-1]

        all_unique_species = []

        for inds in interface_atom_inds:
            layer_atom_coords = frac_coords[inds]
            layer_atom_coords = (layer_atom_coords - supercell_shifts[:,None]).reshape(-1,3)
            inds_in_cell = ((layer_atom_coords[:,:2].round(2) >= 0) & (layer_atom_coords[:,:2].round(2) <= 1)).all(axis=1)
            layer_atom_coords = layer_atom_coords[inds_in_cell].dot(matrix).dot(rot_mat)
            # layer_atom_coords = layer_atom_coords - 
            layer_atom_symbols = np.array(self.interface.species, dtype='str')[inds]
            layer_atom_symbols = np.concatenate([layer_atom_symbols for _ in range(9)])[inds_in_cell]
            layer_atom_species = np.zeros(layer_atom_symbols.shape, dtype=int)
            layer_atom_sizes = np.zeros(layer_atom_symbols.shape, dtype=float)
            unique_species = np.unique(layer_atom_symbols)
            unique_elements = [Element(i) for i in unique_species]


            for i, z in enumerate(unique_elements):
                layer_atom_species[np.isin(layer_atom_symbols, unique_species[i])] = z.Z
                layer_atom_sizes[np.isin(layer_atom_symbols, unique_species[i])] = z.atomic_radius


            all_unique_species.append(np.unique(layer_atom_species))
            colors = vesta_colors[layer_atom_species]
            layer_atom_sizes = vesta_radii[layer_atom_species] * 0.4

            for xy, r, c in zip(layer_atom_coords, layer_atom_sizes, colors):
                ax.add_patch(Circle(xy, radius=r, ec=c[:3], fc=c, linewidth=1.5, clip_on=False))

        ax.plot(
            borders[:,0],
            borders[:,1],
            color='black',
            linewidth=2,
            solid_capstyle='round'
        )
        ax.set_aspect('equal')
        ax.axis('off')

        all_unique_species = np.unique(np.concatenate(all_unique_species))

        legend_lines = []
        legend_labels = []
        for z in all_unique_species:
            legend_lines.append(plt.scatter(
                [np.nan],
                [np.nan],
                color=vesta_colors[z],
                s=50*vesta_radii[z]**2,
                ec=vesta_colors[z,:3],
            ))
            legend_labels.append(
                f'{Element.from_Z(z)}'
            )

        from mpl_toolkits.axes_grid1 import make_axes_locatable
        divider = make_axes_locatable(ax)
        lax = divider.append_axes("bottom", size="8%", pad=0.05)
        lax.axis('off')

        l = lax.legend(
            legend_lines,
            legend_labels,
            ncol=len(all_unique_species),
            loc='center',
            framealpha=1,
            fontsize=legend_fontsize,
            handletextpad=0,
            columnspacing=0.5,
            mode='expand',
        )
        l.set_zorder(200)
        frame = l.get_frame()
        frame.set_facecolor('white')
        frame.set_edgecolor('black')
        frame.set_linewidth(2)

        fig.tight_layout(pad=0.4)
        fig.savefig(output, transparent=transparent)

    
    def create_ga_candidate(
        self,
        film_random_layers,
        sub_random_layers,
        film_shift_layers,
        sub_shift_layers,
        scaling_matrix=[1,1,1],
        atol=None,
    ):
        interface = self.interface
        
        if np.sum(scaling_matrix) > 3:
            interface.make_supercell(scaling_matrix=scaling_matrix)

        species = np.array(interface.species, dtype=str)

        layer_inds, heights = group_layers(interface, atol=atol)
        bot_film_ind = np.min(np.where(heights > self.interface_height))
        top_sub_ind = np.max(np.where(heights < self.interface_height))
        film_random_layer_inds = [bot_film_ind + i for i in range(film_random_layers)]
        sub_random_layer_inds = [top_sub_ind - i for i in range(sub_random_layers)]
        film_shift_layer_inds = [bot_film_ind + (i + film_random_layers) for i in range(film_shift_layers)]
        sub_shift_layer_inds = [top_sub_ind - (i + sub_random_layers) for i in range(film_shift_layers)]

        film_random_inds = np.concatenate(
            [layer_inds[i] for i in film_random_layer_inds]
        )
        sub_random_inds = np.concatenate(
            [layer_inds[i] for i in sub_random_layer_inds]
        )

        film_shift_inds = np.concatenate(
            [layer_inds[i] for i in film_shift_layer_inds]
        )

        sub_shift_inds = np.concatenate(
            [layer_inds[i] for i in sub_shift_layer_inds]
        )

        film_max_shifts = 0.2 * np.concatenate([
            [1 / (j + 1)] * len(layer_inds[i]) for j, i in enumerate(film_shift_layer_inds)
        ])

        sub_max_shifts = 0.2 * np.concatenate([
            [1 / (j + 1)] * len(layer_inds[i]) for j, i in enumerate(sub_shift_layer_inds)
        ])

        random_max = np.max(interface.frac_coords[film_random_inds,-1])
        random_min = np.min(interface.frac_coords[sub_random_inds,-1])

        # random_unit_cell = interface.lattice.matrix.dot(np.array([1,1,(random_max - random_min)]) * np.eye(3))
        random_unit_cell = (np.array([1,1,(random_max - random_min)]) * np.eye(3)).dot(interface.lattice.matrix)

        all_random_inds = np.concatenate([film_random_inds, sub_random_inds])

        blocks = species[all_random_inds]

        unique_e, counts = np.unique(blocks, return_counts=True)

        # Generate a dictionary with the closest allowed interatomic distances
        blmin = closest_distances_generator(
            atom_numbers=[atomic_numbers[i] for i in unique_e],
            ratio_of_covalent_radii=0.75,
        )
        # print(blmin)

        slab = Atoms(cell=random_unit_cell, pbc=True)

        # Initialize the random structure generator
        sg = StartGenerator(
            slab,
            blocks,
            blmin,
            number_of_variable_cell_vectors=0,
        )

        random_atoms = sg.get_new_candidate()
        random_structure = AseAtomsAdaptor().get_structure(random_atoms)

        initial_atoms_ind = np.ones(len(interface), dtype=bool)
        initial_atoms_ind[all_random_inds] = False

        new_species = species[initial_atoms_ind]

        new_frac_coords = copy.deepcopy(interface.frac_coords)
        film_shifts = np.vstack([
            np.random.uniform(low=-i, high=i, size=(1,3)) for i in film_max_shifts
        ]).dot(interface.lattice.inv_matrix)

        sub_shifts = np.vstack([
            np.random.uniform(low=-i, high=i, size=(1,3)) for i in sub_max_shifts
        ]).dot(interface.lattice.inv_matrix)
        new_frac_coords[film_shift_inds] += film_shifts
        new_frac_coords[sub_shift_inds] += sub_shifts

        new_frac_coords = new_frac_coords[initial_atoms_ind]

        new_species = np.concatenate([
            new_species,
            np.array(random_structure.species, dtype=str)
        ])
        random_frac_coords = random_structure.frac_coords
        full_c = np.linalg.norm(interface.lattice.matrix[2])
        small_c = np.linalg.norm(random_structure.lattice.matrix[2])
        random_frac_coords[:, -1] *= (small_c / full_c)
        random_frac_coords[:,-1] += random_min 
        new_frac_coords = np.vstack([
            new_frac_coords,
            random_frac_coords
        ])

        new_structure = Structure(
            lattice=interface.lattice,
            species=new_species,
            coords=new_frac_coords,
            coords_are_cartesian=False,
            to_unit_cell=True,
        )

        return new_structure, random_structure

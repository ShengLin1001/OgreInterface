from OgreInterface.score_function.ewald import EnergyEwald
from OgreInterface.score_function.born import EnergyBorn
from OgreInterface.score_function.generate_inputs import generate_dict_torch
from OgreInterface.surfaces import Interface
from pymatgen.io.ase import AseAtomsAdaptor
from pymatgen.core.periodic_table import Element
from ase.data import atomic_numbers
from typing import Dict
import numpy as np
from matplotlib.colors import Normalize
import matplotlib.pyplot as plt
from mpl_toolkits.axes_grid1 import make_axes_locatable
from scipy.interpolate import RectBivariateSpline
from copy import deepcopy


class IonicSurfaceMatcher:
    def __init__(
        self,
        interface: Interface,
        grid_density_x: int = 15,
        grid_density_y: int = 15,
    ):
        self.interface = interface
        self.matrix = deepcopy(interface.interface.lattice.matrix)
        self._vol = np.linalg.det(self.matrix)

        if self._vol < 0:
            self.matrix *= -1
            self._vol *= -1

        self.cutoff, self.alpha, self.k_max = self._get_ewald_parameters()
        self.charge_dict, self.radius_dict = self._get_charges_and_radii()
        self.ns_dict = {element: 5.0 for element in self.charge_dict}

        self.d_interface = self.interface.interfacial_distance
        self.film_part = self.interface.film_part
        self.sub_part = self.interface.sub_part
        self.grid_density_x = grid_density_x
        self.grid_density_y = grid_density_y

        self.shifts, self.X, self.Y = self._generate_shifts()

    def _get_charges_and_radii(self):
        sub = self.interface.substrate.conventional_bulk_structure
        film = self.interface.film.conventional_bulk_structure
        sub_oxidation_state = sub.composition.oxi_state_guesses()[0]
        film_oxidation_state = film.composition.oxi_state_guesses()[0]

        sub_oxidation_state.update(film_oxidation_state)

        radii = {
            element: Element(element).ionic_radii[charge]
            for element, charge in sub_oxidation_state.items()
        }

        return sub_oxidation_state, radii

    def _get_ewald_parameters(self):
        struc_vol = self.interface.structure_volume
        accf = np.sqrt(np.log(10**4))
        w = 1 / 2**0.5
        alpha = np.pi * (
            len(self.interface.interface) * w / (struc_vol**2)
        ) ** (1 / 3)
        cutoff = accf / np.sqrt(alpha)
        k_max = 2 * np.sqrt(alpha) * accf

        return cutoff, alpha, k_max

    def _generate_shifts(self):
        grid_x = np.linspace(0, 1, self.grid_density_x)
        grid_y = np.linspace(0, 1, self.grid_density_y)
        X, Y = np.meshgrid(grid_x, grid_y)

        shifts = np.c_[X.ravel(), Y.ravel(), np.zeros(X.shape).ravel()]

        return shifts, X, Y

    def _get_shifted_atoms(self, shifts):

        atoms = [
            AseAtomsAdaptor().get_atoms(self.sub_part),
            AseAtomsAdaptor().get_atoms(self.film_part),
        ]

        for shift in shifts:
            shifted_atoms = self.interface.shift_film(
                shift, fractional=True, inplace=False, return_atoms=True
            )
            atoms.append(shifted_atoms)

        return atoms

    def _generate_inputs(self, shifts):
        atoms_list = self._get_shifted_atoms(shifts)
        inputs = generate_dict_torch(
            atoms=atoms_list,
            cutoff=self.cutoff,
            charge_dict=self.charge_dict,
            radius_dict=self.radius_dict,
            ns_dict=self.ns_dict,
        )

        return inputs

    def _calculate_coulomb(self, inputs):
        ewald = EnergyEwald(alpha=self.alpha, k_max=self.k_max)
        coulomb_energy = ewald.forward(inputs)

        return coulomb_energy

    def _calculate_born(self, inputs):
        born = EnergyBorn(cutoff=self.cutoff)
        born_energy = born.forward(inputs)

        return born_energy

    def _get_interpolated_data(self, X, Y, Z):
        x_grid = np.linspace(0, 1, self.grid_density_x)
        y_grid = np.linspace(0, 1, self.grid_density_y)
        spline = RectBivariateSpline(y_grid, x_grid, Z)

        x_grid_interp = np.linspace(0, 1, 401)
        y_grid_interp = np.linspace(0, 1, 401)

        X_interp, Y_interp = np.meshgrid(x_grid_interp, y_grid_interp)
        Z_interp = spline.ev(xi=Y_interp, yi=X_interp)
        frac_shifts = np.c_[
            X_interp.ravel(),
            Y_interp.ravel(),
            np.zeros(X_interp.shape).ravel(),
        ]

        cart_shifts = frac_shifts.dot(self.matrix)

        X_cart = cart_shifts[:, 0].reshape(X_interp.shape)
        Y_cart = cart_shifts[:, 1].reshape(Y_interp.shape)

        return X_cart, Y_cart, Z_interp

    def _plot_heatmap(self, fig, ax, X, Y, Z, borders, cmap, fontsize):
        ax.set_xlabel(r"Shift in $x$ ($\AA$)", fontsize=fontsize)
        ax.set_ylabel(r"Shift in $y$ ($\AA$)", fontsize=fontsize)

        im = ax.contourf(
            X,
            Y,
            Z,
            cmap=cmap,
            levels=200,
            norm=Normalize(vmin=np.nanmin(Z), vmax=np.nanmax(Z)),
        )

        ax.plot(
            borders[:, 0],
            borders[:, 1],
            color="black",
            linewidth=2,
        )

        divider = make_axes_locatable(ax)
        cax = divider.append_axes("top", size="5%", pad=0.1)
        cbar = fig.colorbar(im, cax=cax, orientation="horizontal")
        cbar.ax.tick_params(labelsize=fontsize)
        cbar.ax.locator_params(nbins=3)
        cbar.set_label("$E_{adh}$ (eV)", fontsize=fontsize)
        cax.xaxis.set_ticks_position("top")
        cax.xaxis.set_label_position("top")
        ax.tick_params(labelsize=fontsize)
        ax.set_xlim(borders[:, 0].min(), borders[:, 0].max())
        ax.set_ylim(borders[:, 1].min(), borders[:, 1].max())
        ax.set_aspect("equal")

    def run_surface_matching(
        self,
        cmap: str = "jet",
        fontsize: int = 14,
        output: str = "PES.png",
        shift: bool = True,
    ) -> None:
        shifts = self.shifts
        inputs = self._generate_inputs(shifts)
        coulomb_energy = self._calculate_coulomb(inputs)
        born_energy = self._calculate_born(inputs)

        sub_coulomb_energy = coulomb_energy[0]
        film_coulomb_energy = coulomb_energy[1]
        interface_coulomb_energy = coulomb_energy[2:].reshape(self.X.shape)

        sub_born_energy = born_energy[0]
        film_born_energy = born_energy[1]
        interface_born_energy = born_energy[2:].reshape(self.X.shape)

        coulomb_adh_energy = (
            sub_coulomb_energy + film_coulomb_energy
        ) - interface_coulomb_energy
        born_adh_energy = (
            sub_born_energy + film_born_energy
        ) - interface_born_energy

        X_plot, Y_plot, Z_born = self._get_interpolated_data(
            self.X, self.Y, born_adh_energy
        )
        _, _, Z_coulomb = self._get_interpolated_data(
            self.X, self.Y, coulomb_adh_energy
        )

        Z_both = Z_born + Z_coulomb

        a = self.matrix[0, :2]
        b = self.matrix[1, :2]
        borders = np.vstack([np.zeros(2), a, a + b, b, np.zeros(2)])
        x_size = borders[:, 0].max() - borders[:, 0].min()
        y_size = borders[:, 1].max() - borders[:, 1].min()
        ratio = y_size / x_size

        fig, (ax1, ax2, ax3) = plt.subplots(
            figsize=(3 * 5, 5 * ratio),
            ncols=3,
            dpi=400,
        )

        self._plot_heatmap(
            fig=fig,
            ax=ax1,
            X=X_plot,
            Y=Y_plot,
            Z=Z_born,
            borders=borders,
            cmap=cmap,
            fontsize=fontsize,
        )
        self._plot_heatmap(
            fig=fig,
            ax=ax2,
            X=X_plot,
            Y=Y_plot,
            Z=Z_coulomb,
            borders=borders,
            cmap=cmap,
            fontsize=fontsize,
        )
        self._plot_heatmap(
            fig=fig,
            ax=ax3,
            X=X_plot,
            Y=Y_plot,
            Z=Z_both,
            borders=borders,
            cmap=cmap,
            fontsize=fontsize,
        )

        frac_shifts = np.c_[
            X_plot.ravel(), Y_plot.ravel(), np.zeros(Y_plot.shape).ravel()
        ].dot(np.linalg.inv(self.matrix))
        opt_shift = frac_shifts[np.argmax(Z_both.ravel())]
        plot_shift = opt_shift.dot(self.matrix)

        ax3.scatter(
            [plot_shift[0]],
            [plot_shift[1]],
            fc="white",
            ec="black",
            marker="X",
            s=100,
            zorder=10,
        )

        if shift:
            self.interface.shift_film(
                shift=opt_shift, fractional=True, inplace=True
            )

        fig.tight_layout()
        fig.savefig(output, bbox_inches="tight")

    def run_z_shifts(
        self,
        interfacial_distances: np.ndarray = np.linspace(2.0, 4.0, 101),
        fontsize: int = 12,
        output: str = "z_shift.png",
    ) -> None:
        z_shifts = interfacial_distances - self.d_interface
        shifts = np.c_[
            np.zeros((len(z_shifts), 2)), z_shifts
        ] / np.linalg.norm(self.matrix[-1])
        inputs = self._generate_inputs(shifts)
        coulomb_energy = self._calculate_coulomb(inputs)
        born_energy = self._calculate_born(inputs)

        sub_coulomb_energy = coulomb_energy[0]
        film_coulomb_energy = coulomb_energy[1]
        interface_coulomb_energy = coulomb_energy[2:]

        sub_born_energy = born_energy[0]
        film_born_energy = born_energy[1]
        interface_born_energy = born_energy[2:]

        coulomb_adh_energy = (
            sub_coulomb_energy + film_coulomb_energy
        ) - interface_coulomb_energy
        born_adh_energy = (
            sub_born_energy + film_born_energy
        ) - interface_born_energy

        both_adh_energy = born_adh_energy + coulomb_adh_energy

        fig, ax = plt.subplots(figsize=(4, 4), dpi=400)
        ax.set_xlabel("Interfacial Distance ($\\AA$)", fontsize=fontsize)
        ax.set_ylabel("$E_{adh}$", fontsize=fontsize)
        ax.plot(interfacial_distances, both_adh_energy, color="red")
        ax.set_xlim(interfacial_distances.min(), interfacial_distances.max())

        fig.tight_layout(pad=0.4)
        fig.savefig(output)

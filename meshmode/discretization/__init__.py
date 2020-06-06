__copyright__ = "Copyright (C) 2013-2020 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

import numpy as np

from pytools import memoize_in, memoize_method
from pytools.obj_array import make_obj_array
from meshmode.array_context import ArrayContext, make_loopy_program

# underscored because it shouldn't be imported from here.
from meshmode.dof_array import DOFArray as _DOFArray

__doc__ = """
.. autoclass:: ElementGroupBase
.. autoclass:: InterpolatoryElementGroupBase
.. autoclass:: ElementGroupFactory
.. autoclass:: Discretization
"""


# {{{ element group base

class NoninterpolatoryElementGroupError(TypeError):
    pass


class ElementGroupBase(object):
    """Container for the :class:`Discretization` data corresponding to
    one :class:`meshmode.mesh.MeshElementGroup`.

    .. attribute :: mesh_el_group
    .. attribute :: order
    .. attribute :: index

    .. autoattribute:: nelements
    .. autoattribute:: nunit_dofs
    .. autoattribute:: ndofs
    .. autoattribute:: dim
    .. automethod:: view

    .. method:: unit_nodes()

        Returns a :class:`numpy.ndarray` of shape ``(dim, nunit_dofs)``
        of reference coordinates of interpolation nodes.

    .. method:: weights()

        Returns an array of length :attr:`nunit_dofs` containing
        quadrature weights.

    .. attribute:: is_affine

        A :class:`bool` flag that is *True* if the local-to-global
        parametrization of all the elements in the group is affine. Based on
        :attr:`meshmode.mesh.MeshElementGroup.is_affine`.
    """

    def __init__(self, mesh_el_group, order, index):
        """
        :arg mesh_el_group: an instance of
            :class:`meshmode.mesh.MeshElementGroup`
        """
        self.mesh_el_group = mesh_el_group
        self.order = order
        self.index = index

    @property
    def is_affine(self):
        return self.mesh_el_group.is_affine

    @property
    def nelements(self):
        return self.mesh_el_group.nelements

    @property
    def nunit_dofs(self):
        return self.unit_nodes.shape[-1]

    @property
    def ndofs(self):
        return self.nunit_dofs * self.nelements

    @property
    def dim(self):
        return self.mesh_el_group.dim

    def basis(self):
        raise NoninterpolatoryElementGroupError("'{}' "
                "is not equipped with a unisolvent function space "
                "and therefore cannot be used for interpolation"
                .format(self.__class__.__name__))

    grad_basis = basis
    diff_matrices = basis

# }}}


# {{{ interpolatory element group base

class InterpolatoryElementGroupBase(ElementGroupBase):
    """A subclass of :class:`ElementGroupBase` that is equipped with a
    function space.

    .. method:: basis()

        Returns a :class:`list` of basis functions that take arrays
        of shape ``(dim, n)`` and return an array of shape (n,)``
        (which performs evaluation of the basis function).

    .. method:: grad_basis()

        :returns: a :class:`tuple` of functions, each of which
            accepts arrays of shape *(dims, npts)* and returns a
            :class:`tuple` of length *dims* containing the
            derivatives along each axis as an array of size
            *npts*.  'Scalar' evaluation, by passing just one
            vector of length *dims*, is also supported.

    .. method:: diff_matrices()

        Return a :attr:`dim`-long :class:`tuple` of matrices of
        shape ``(nunit_nodes, nunit_nodes)``, each of which,
        when applied to an array of nodal values, take derivatives
        in the reference (r,s,t) directions.
    """

# }}}


# {{{ group factories

class ElementGroupFactory(object):
    """
    .. function:: __call__(mesh_ele_group, node_nr_base)
    """


class OrderBasedGroupFactory(ElementGroupFactory):
    def __init__(self, order):
        self.order = order

    def __call__(self, mesh_el_group, node_nr_base):
        if not isinstance(mesh_el_group, self.mesh_group_class):
            raise TypeError("only mesh element groups of type '%s' "
                    "are supported" % self.mesh_group_class.__name__)

        return self.group_class(mesh_el_group, self.order, node_nr_base)


# }}}


class Discretization(object):
    """An unstructured composite discretization.

    .. attribute:: real_dtype

    .. attribute:: complex_dtype

    .. attribute:: mesh

    .. attribute:: dim

    .. attribute:: ambient_dim

    .. attribute :: groups

    .. automethod:: empty
    .. automethod:: zeros
    .. automethod:: empty_like
    .. automethod:: zeros_like

    .. automethod:: nodes()

    .. method:: num_reference_derivative(queue, ref_axes, vec)

    .. method:: quad_weights(queue)

        A :class:`~meshmode.dof_array.DOFArray` with quadrature weights.
    """

    def __init__(self, actx: ArrayContext, mesh, group_factory,
            real_dtype=np.float64):
        """
        :param actx: A :class:`ArrayContext` used to perform computation needed
            during initial set-up of the mesh.
        :param mesh: A :class:`meshmode.mesh.Mesh` over which the discretization is
            built
        :param group_factory: An :class:`ElementGroupFactory`
        :param real_dtype: The :mod:`numpy` data type used for representing real
            data, either :class:`numpy.float32` or :class:`numpy.float64`
        """

        if not isinstance(actx, ArrayContext):
            raise TypeError("'actx' must be an ArrayContext")

        self.mesh = mesh
        groups = []
        for mg in mesh.groups:
            ng = group_factory(mg, len(groups))
            groups.append(ng)

        self.groups = groups

        self.real_dtype = np.dtype(real_dtype)
        self.complex_dtype = np.dtype({
                np.float32: np.complex64,
                np.float64: np.complex128
                }[self.real_dtype.type])

        self._setup_actx = actx

    @property
    def dim(self):
        return self.mesh.dim

    @property
    def ambient_dim(self):
        return self.mesh.ambient_dim

    def _new_array(self, actx, creation_func, dtype=None):
        if dtype is None:
            dtype = self.real_dtype
        elif dtype == "c":
            dtype = self.complex_dtype
        else:
            dtype = np.dtype(dtype)

        return _DOFArray.from_list(actx, [
            creation_func(shape=(grp.nelements, grp.nunit_dofs), dtype=dtype)
            for grp in self.groups])

    def empty(self, actx: ArrayContext, dtype=None):
        """Return an empty :class:`~meshmode.dof_array.DOFArray`.

        :arg dtype: type special value 'c' will result in a
            vector of dtype :attr:`self.complex_dtype`. If
            *None* (the default), a real vector will be returned.
        """
        if not isinstance(actx, ArrayContext):
            raise TypeError("'actx' must be an ArrayContext, not '%s'"
                    % type(actx).__name__)

        return self._new_array(actx, actx.empty, dtype=dtype)

    def zeros(self, actx: ArrayContext, dtype=None):
        """Return a zero-initialized :class:`~meshmode.dof_array.DOFArray`.

        :arg dtype: type special value 'c' will result in a
            vector of dtype :attr:`self.complex_dtype`. If
            *None* (the default), a real vector will be returned.
        """
        if not isinstance(actx, ArrayContext):
            raise TypeError("'actx' must be an ArrayContext, not '%s'"
                    % type(actx).__name__)

        return self._new_array(actx, actx.zeros, dtype=dtype)

    def empty_like(self, array: _DOFArray):
        return self.empty(array.array_context, dtype=array.entry_dtype)

    def zeros_like(self, array: _DOFArray):
        return self.zeros(array.array_context, dtype=array.entry_dtype)

    def num_reference_derivative(self, ref_axes, vec):
        @memoize_in(self, "reference_derivative_prg")
        def prg():
            return make_loopy_program(
                """{[iel,idof,j]:
                    0<=iel<nelements and
                    0<=idof,j<nunit_dofs}""",
                "result[iel,idof] = sum(j, diff_mat[idof, j] * vec[iel, j])",
                name="diff")

        actx = vec.array_context

        def get_mat(grp):
            mat = None
            for ref_axis in ref_axes:
                next_mat = grp.diff_matrices()[ref_axis]
                if mat is None:
                    mat = next_mat
                else:
                    mat = np.dot(next_mat, mat)

            return mat

        return _DOFArray.from_list(actx, [
                actx.call_loopy(
                    prg(), diff_mat=actx.from_numpy(get_mat(grp)), vec=vec[grp.index]
                    )["result"]
                for grp in self.groups])

    @memoize_method
    def quad_weights(self):
        @memoize_in(self, "quad_weights_prg")
        def prg():
            return make_loopy_program(
                "{[iel,idof]: 0<=iel<nelements and 0<=idof<nunit_dofs}",
                "result[iel,idof] = weights[idof]",
                name="quad_weights")

        actx = self._setup_actx

        return _DOFArray(None, [
                actx.freeze(
                    actx.call_loopy(
                        prg(), weights=actx.from_numpy(grp.weights)
                        )["result"])
                for grp in self.groups])

    @memoize_method
    def nodes(self):
        """
        :returns: object array of shape ``(ambient_dim,)`` containing
            :class:`~meshmode.dof_array.DOFArray`s of node coordinates.
        """

        @memoize_in(self, "nodes_prg")
        def prg():
            return make_loopy_program(
                """{[iel,idof,j]:
                    0<=iel<nelements and
                    0<=idof<ndiscr_nodes and
                    0<=j<nmesh_nodes}""",
                """
                    result[iel, idof] = \
                        sum(j, resampling_mat[idof, j] * nodes[iel, j])
                    """,
                name="nodes")

        actx = self._setup_actx

        return make_obj_array([
            _DOFArray.from_list(None, [
                actx.freeze(
                    actx.call_loopy(
                        prg(),
                        resampling_mat=actx.from_numpy(
                            grp.from_mesh_interp_matrix()),
                        nodes=actx.from_numpy(grp.mesh_el_group.nodes[iaxis])
                        )["result"])
                for grp in self.groups])
            for iaxis in range(self.ambient_dim)])

# vim: fdm=marker

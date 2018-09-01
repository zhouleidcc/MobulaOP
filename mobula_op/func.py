import ctypes
import functools
import os
import sys
import inspect
from . import glue
from .dtype import DType

class DLLWrapper:
    def __init__(self, dll_fname):
        self.dll = ctypes.CDLL(dll_fname)
    def get_function(self, name, argtypes):
        # [TODO] Check Types
        return getattr(self.dll, name)
    def __getattr__(self, name):
        return getattr(self.dll, name)

class MobulaFuncLib:
    FUNC_LIB_CACHE = dict()
    def __init__(self, lib_path = None, force = True):
        if lib_path is None:
            lib_path = os.path.join(os.path.dirname(__file__), 'build/mobula_op')
        cpu_lib_fname = "%s_cpu.so" % lib_path
        gpu_lib_fname = "%s_gpu.so" % lib_path
        self.cpu_lib = self.load_dll(cpu_lib_fname, force)
        self.gpu_lib = self.load_dll(gpu_lib_fname, False)
    @staticmethod
    def load_dll(dll_fname, force):
        if dll_fname in MobulaFuncLib.FUNC_LIB_CACHE:
            return MobulaFuncLib.FUNC_LIB_CACHE[dll_fname]
        if os.path.exists(dll_fname):
            dll = DLLWrapper(dll_fname)
            MobulaFuncLib.FUNC_LIB_CACHE[dll_fname] = dll
            return dll
        elif force:
            raise IOError('{} not found'.format(dll_fname))
        return None

default_func_lib = MobulaFuncLib(force = False)

class CArray(ctypes.Structure):
    _fields_ = [('size', ctypes.c_size_t), ('data', ctypes.c_void_p)]

class CFuncDef:
    def __init__(self, func_name, arg_names=[], arg_types=None, rtn_type=None, lib_path=None):
        self.func_name = func_name
        self.arg_names = arg_names
        self.arg_types = arg_types
        self.rtn_type = rtn_type
        if lib_path is None:
            self.func_lib = default_func_lib
        elif type(lib_path) == str:
            self.func_lib = MobulaFuncLib(lib_path)
        else:
            self.func_lib = lib_path

def type_to_ctype(p):
    if isinstance(p, (list, tuple)):
        return CArray
    elif isinstance(p, DType):
        return p.ctype
    elif p is None:
        return None
    else:
        raise TypeError("Unsupported Type: {}".format(p))

def types_to_ctypes(par_types):
    return [type_to_ctype(p) for p in par_types]

class MobulaFunc:
    def __init__(self, name, func):
        self.name = name
        if isinstance(func, (list, tuple)):
            alias, func = func
            self.name_in_lib = alias
        else:
            self.name_in_lib = name
        self.func_lib = func.func_lib
        self.par_name = func.arg_names
        self.par_type = func.arg_types
        # register type
        for lib in [self.func_lib.cpu_lib, self.func_lib.gpu_lib]:
            if lib is not None:
                libf = getattr(lib, self.name_in_lib)
                libf.restype = type_to_ctype(func.rtn_type)
                libf.argtypes = types_to_ctypes(func.arg_types)
    def __call__(self, *args, **kwargs):
        def args_gen():
            i = 0
            for a in args:
                yield a
                i += 1
            num_pars = len(self.par_name)
            while i < num_pars:
                yield kwargs[self.par_name[i]]
                i += 1

        # type check
        args_new = []
        backend = None
        dev_id = None
        noncontiguous_list = []
        temp_list = []
        backend_inputs = []
        backend_outputs = []
        argtypes = []

        def analyze_element(a, p, backend, backend_inputs, backend_outputs, noncontiguous_list):
            assert isinstance(p, DType)
            if p.is_pointer:
                # multiple-dim array
                if p.is_const:
                    backend_inputs.append(a)
                else:
                    backend_outputs.append(a)

                pa = backend.get_pointer(a)
                if isinstance(pa, (list, tuple)):
                    if p.is_const:
                        temp_list.append(pa[1]) # hold a reference
                    else:
                        noncontiguous_list.append((a, pa[1]))
                    pa = pa[0]
                dev_id = backend.dev_id(a)
                ctype = backend.get_ctype(a)
                pa = ctypes.cast(pa, ctypes.POINTER(ctype))
            else:
                pa = a
                dev_id = None
                ctype = p
            return pa, dev_id, ctype

        # Pre-Check
        def check_backend(a, backend):
            backend_tmp = glue.backend.get_var_backend(a)
            if backend_tmp is not None and backend is not None and backend_tmp != backend:
                raise ValueError("Don't use multiple backends in a call :-( %s vs %s" % (backend, backend_tmp))
            return backend_tmp

        for a, p in zip(args_gen(), self.par_type):
            if isinstance(p, (list, tuple)):
                for e in a:
                    backend = check_backend(e, backend)
            else:
                backend = check_backend(a, backend)

        extra_pars = [backend, backend_inputs, backend_outputs, noncontiguous_list]

        for a, p in zip(args_gen(), self.par_type):
            if isinstance(p, (list, tuple)):
                # List or Tuple Object
                ep = p[0]
                analysis = [analyze_element(e, ep, *extra_pars) for e in a]
                pas = [a[0] for a in analysis]

                for a in analysis:
                    aid = a[1]
                    if aid is not None:
                        if dev_id is not None:
                            assert aid == dev_id, ValueError("Don't use multiple devices in a call :-(")
                        else:
                            dev_id = aid

                print ("EPEP", ep)
                if ep == int:
                    ctype = ctypes.c_int
                elif ep == float:
                    ctype = ctypes.c_float
                elif ep in [IN, OUT]:
                    ctype = ctypes.c_void_p
                ca = CArray()
                ca.size = len(pas)
                ca.data = ctypes.cast((ctype * len(pas))(*pas), ctypes.c_void_p)
                args_new.append(ca)
                argtypes.append([ctype])
            else:
                pa, aid, ctype = analyze_element(a, p, *extra_pars)
                args_new.append(pa)
                argtypes.append(ctype)

                if aid is not None:
                    if dev_id is not None:
                        assert aid == dev_id, ValueError("Don't use multiple devices in a call :-(")
                    else:
                        dev_id = aid

        if backend is not None:
            assert backend is not None, ValueError("No parameter about backend:-(")
            backend.wait_to_read(backend_inputs)
            backend.wait_to_write(backend_outputs)

        if dev_id is not None:
            if self.func_lib.gpu_lib is None:
                raise RuntimeError("Doesn't support GPU")
            self.func_lib.gpu_lib.set_device(dev_id)
            lib = self.func_lib.gpu_lib
        else:
            lib = self.func_lib.cpu_lib

        func = lib.get_function(self.name_in_lib, argtypes)
        rtn = func(*args_new)

        for source, target in noncontiguous_list:
            source[:] = target
        return rtn

def bind(functions):
    for k, func in functions.items():
        assert k not in globals(), "Duplicated function name %s" % k # function overload [todo]
        alias = None
        if isinstance(func, (list, tuple)):
            alias, func = func
        if not isinstance(func, CFuncDef):

            spec = glue.common.getargspec(func)
            assert len(spec.args) == len(spec.defaults), ValueError('Function %s should specify type for each parameter')
            func = CFuncDef(func_name=k, arg_names=spec.args, arg_types=spec.defaults, rtn_type=func())

        if alias is not None:
            func = (alias, func)

        globals()[k] = MobulaFunc(k, func)

def get_3loop_size(shape, axis):
    # return: outer_size, middle_size, inner_size
    len_shape = len(shape)
    if axis < 0:
        axis += len_shape
    assert 0 <= axis < len_shape
    outer_size = 1
    for i in range(0, axis):
        outer_size *= shape[i]
    inner_size = 1
    for i in range(axis + 1, len_shape):
        inner_size *= shape[i]
    return outer_size, shape[axis], inner_size

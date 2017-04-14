import logging
import re
from abc import abstractmethod, ABCMeta
from collections import defaultdict
from functools import partial

import ctapipe
import numpy as np
import tables
from astropy.units import Quantity

__all__ = ['SimpleHDF5TableWriter',
           'tr_convert_and_strip_unit',
           'tr_list_to_mask']

log = logging.getLogger(__name__)

PYTABLES_TYPE_MAP = {
    'float': tables.Float64Col,
    'float64': tables.Float64Col,
    'float32': tables.Float32Col,
    'int': tables.IntCol,
    'bool': tables.BoolCol,
}


class TableWriter(metaclass=ABCMeta):
    def __init__(self):
        self._transforms = defaultdict(dict)
        self._exclusions = defaultdict(list)

    def exclude(self, table_name, pattern):
        """
        Exclude any columns matching the pattern from being written
        
        Parameters
        ----------
        table_name: str 
            name of table on which to apply the exclusion
        pattern: str
            regular expression string to match column name
        """
        self._exclusions[table_name].append(re.compile(pattern))

    def _is_column_excluded(self, table_name, col_name):
        for pattern in self._exclusions[table_name]:
            if pattern.match(col_name):
                return True
        return False

    def add_column_transform(self, table_name, col_name, transform):
        """
        Add a transformation function for a column. This function will be 
        called on the value in the container before it is written to the 
        output file. 
        
        Parameters
        ----------
        table_name: str
            identifier of table being written
        col_name: str
            name of column in the table (or item in the Container)
        transform: callable
            function that take a value and returns a new one 
        """
        self._transforms[table_name][col_name] = transform
        log.debug("Added transform: {}/{} -> {}".format(table_name, col_name,
                                                        transform))

    @abstractmethod
    def write(self, table_name, container):
        """
        Write the contents of the given container to a table.  The first call 
        to write  will create a schema and initialize the table within the 
        file. The shape of data within the container must not change between 
        calls, since variable-length arrays are not supported. 
    
        Parameters
        ----------
        table_name: str 
            name of table to write to 
        container: `ctapipe.core.Container` 
            container to write
        """
        pass


class SimpleHDF5TableWriter(TableWriter):
    """
    A very basic table writer that can take a container (or more than one) 
    and write it to an HDF5 file. It does _not_ recursively write the 
    container. This is intended as a building block to create a more complex 
    I/O system. 
    
    It works by creating a HDF5 Table description from the `Items` inside a 
    container, where each item becomes a column in the table. The first time 
    `SimpleHDF5TableWriter.write()` is called, the container is registered 
    and the table created in the output file. 
    
    Each item in the container can also have an optional transform function 
    that is called before writing to transform the value.  For example, 
    unit quantities always have their units removed, or converted to a 
    common unit if specified in the `Item`. 
    
    Any metadata in the `Container` (stored in `Container.meta`) will be 
    written to the table's header on the first call to write() 
    
    Multiple tables may be written at once in a single file, as long as you 
    change the table_name attribute to write() to specify which one to write 
    to. 
    
    Parameters:
    -----------
    filename: str
        name of hdf5 output file
    group_name: str
        name of group into which to put all of the tables generated by this 
        Writer (it will be placed under "/" in the file)
        
    """

    def __init__(self, filename, group_name):
        super().__init__()
        self._schemas = {}
        self._tables = {}
        self._h5file = tables.open_file(filename, mode="w")
        self._group = self._h5file.create_group("/", group_name)

    def __del__(self):
        self._h5file.close()

    def _create_hdf5_table_schema(self, table_name, container):
        """
        Creates a pytables description class for a container 
        and registers it in the Writer
        
        Parameters
        ----------
        table_name: str
            name of table
        container: ctapipe.core.Container
            instance of an initalized container

        Returns
        -------
        dictionary of extra metadata to add to the table's header
        """

        class Schema(tables.IsDescription):
            pass

        meta = {}  # any extra meta-data generated here (like units, etc)

        # create pytables schema description for the given container
        for col_name, value in container.items():

            typename = ""
            shape = 1

            if self._is_column_excluded(table_name, col_name):
                log.debug("excluded column: {}/{}".format(table_name,col_name))
                continue

            if isinstance(value, Quantity):
                req_unit = container.attributes[col_name].unit
                if req_unit is not None:
                    tr = partial(tr_convert_and_strip_unit, unit=req_unit)
                    meta['{}_UNIT'.format(col_name)] = str(req_unit)
                else:
                    tr = lambda x: x.value
                    meta['{}_UNIT'.format(col_name)] = str(value.unit)

                value = tr(value)
                self.add_column_transform(table_name, col_name, tr)

            if isinstance(value, np.ndarray):
                typename = value.dtype.name
                coltype = PYTABLES_TYPE_MAP[typename]
                shape = value.shape
                Schema.columns[col_name] = coltype(shape=shape)

            elif type(value).__name__ in PYTABLES_TYPE_MAP:
                typename = type(value).__name__
                coltype = PYTABLES_TYPE_MAP[typename]
                Schema.columns[col_name] = coltype()

            log.debug("Table {}: added col: {} type: {} shape: {}"
                      .format(table_name, col_name, typename, shape))

        self._schemas[table_name] = Schema
        meta['CTAPIPE_VERSION'] = ctapipe.__version__
        return meta


    def _setup_new_table(self, table_name, container):
        """ set up the table. This is called the first time `write()` 
        is called on a new table """
        log.debug("Initializing table '{}'".format(table_name))
        meta = self._create_hdf5_table_schema(table_name, container)
        meta.update(container.meta) # copy metadata from container

        table = self._h5file.create_table(where=self._group,
                                          name=table_name,
                                          title=container.__class__.__name__,
                                          description=self._schemas[table_name])
        for key, val in meta.items():
            table.attrs[key] = val

        self._tables[table_name] = table


    def _append_row(self, table_name, container):
        """
        append a row to an already initialized table. This is called 
        automatically by `write()`
        """
        table = self._tables[table_name]
        row = table.row

        for colname in table.colnames:

            value = container[colname]

            # apply value transform function if it exists for this column
            if colname in self._transforms[table_name]:
                tr = self._transforms[table_name][colname]
                value = tr(value)

            row[colname] = value

        row.append()

    def write(self, table_name, container):
        """
        Write the contents of the given container to a table.  The first call 
        to write  will create a schema and initialize the table within the 
        file. The shape of data within the container must not change between 
        calls, since variable-length arrays are not supported. 
        
        Parameters
        ----------
        table_name: str 
            name of table to write to 
        container: `ctapipe.core.Container` 
            container to write
        """

        if table_name not in self._schemas:
            self._setup_new_table(table_name, container)

        self._append_row(table_name, container)

def tr_convert_and_strip_unit(quantity, unit):
    return quantity.to(unit).value

def tr_list_to_mask(thelist, length):
    """ transform list to a fixed-length mask"""
    arr = np.zeros(shape=length, dtype=np.bool)
    arr[thelist] = True
    return arr
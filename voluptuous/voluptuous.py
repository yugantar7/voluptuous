# encoding: utf-8
#
# Copyright (C) 2010-2013 Alec Thomas <alec@swapoff.org>
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution.
#
# Author: Alec Thomas <alec@swapoff.org>

"""Schema validation for Python data structures.

Given eg. a nested data structure like this:

    {
        'exclude': ['Users', 'Uptime'],
        'include': [],
        'set': {
            'snmp_community': 'public',
            'snmp_timeout': 15,
            'snmp_version': '2c',
        },
        'targets': {
            'localhost': {
                'exclude': ['Uptime'],
                'features': {
                    'Uptime': {
                        'retries': 3,
                    },
                    'Users': {
                        'snmp_community': 'monkey',
                        'snmp_port': 15,
                    },
                },
                'include': ['Users'],
                'set': {
                    'snmp_community': 'monkeys',
                },
            },
        },
    }

A schema like this:

    >>> settings = {
    ...   'snmp_community': str,
    ...   'retries': int,
    ...   'snmp_version': All(Coerce(str), Any('3', '2c', '1')),
    ... }
    >>> features = ['Ping', 'Uptime', 'Http']
    >>> schema = Schema({
    ...    'exclude': features,
    ...    'include': features,
    ...    'set': settings,
    ...    'targets': {
    ...      'exclude': features,
    ...      'include': features,
    ...      'features': {
    ...        str: settings,
    ...      },
    ...    },
    ... })

Validate like so:

    >>> schema({
    ...   'set': {
    ...     'snmp_community': 'public',
    ...     'snmp_version': '2c',
    ...   },
    ...   'targets': {
    ...     'exclude': ['Ping'],
    ...     'features': {
    ...       'Uptime': {'retries': 3},
    ...       'Users': {'snmp_community': 'monkey'},
    ...     },
    ...   },
    ... })  # doctest: +NORMALIZE_WHITESPACE
    {'set': {'snmp_version': '2c', 'snmp_community': 'public'},
     'targets': {'exclude': ['Ping'],
                 'features': {'Uptime': {'retries': 3},
                              'Users': {'snmp_community': 'monkey'}}}}
"""

from functools import wraps
from itertools import ifilter
import os
import re
import sys
if sys.version > '3':
    import urllib.parse as urlparse
    long = int
    unicode = str
else:
    import urlparse


__author__ = 'Alec Thomas <alec@swapoff.org>'
__version__ = '0.7.1'


class Undefined(object):
    def __nonzero__(self):
        return False

    def __repr__(self):
        return '...'


UNDEFINED = Undefined()


class Error(Exception):
    """Base validation exception."""


class SchemaError(Error):
    """An error was encountered in the schema."""


class Invalid(Error):
    """The data was invalid.

    :attr msg: The error message.
    :attr path: The path to the error, as a list of keys in the source data.
    """

    def __init__(self, message, path=None):
        Error.__init__(self,  message)
        self.path = path or []

    @property
    def msg(self):
        return self.args[0]

    def __str__(self):
        path = ' @ data[%s]' % ']['.join(map(repr, self.path)) \
                if self.path else ''
        return Exception.__str__(self) + path


class MultipleInvalid(Invalid):
    def __init__(self, errors=None):
        self.errors = errors[:] if errors else []

    @property
    def msg(self):
        return self.errors[0].msg

    @property
    def path(self):
        return self.errors[0].path

    def add(self, error):
        self.errors.append(error)

    def __str__(self):
        return str(self.errors[0])


class Schema(object):
    """A validation schema.

    The schema is a Python tree-like structure where nodes are pattern
    matched against corresponding trees of values.

    Nodes can be values, in which case a direct comparison is used, types,
    in which case an isinstance() check is performed, or callables, which will
    validate and optionally convert the value.
    """

    def __init__(self, schema, required=False, extra=False):
        """Create a new Schema.

        :param schema: Validation schema. See :module:`voluptuous` for details.
        :param required: Keys defined in the schema must be in the data.
        :param extra: Keys in the data need not have keys in the schema.
        """
        self.schema = schema
        self.required = required
        self.extra = extra
        self._compiled = self._compile(schema)

    def __call__(self, data):
        """Validate data against this schema."""
        try:
            return self._compiled([], data)
        except MultipleInvalid:
            raise
        except Invalid as e:
            raise MultipleInvalid([e])
        # return self.validate([], self.schema, data)

    def _compile(self, schema):
        if schema is Extra:
            return lambda _, v: v
        if isinstance(schema, Object):
            return self._compile_object(schema)
        if isinstance(schema, dict):
            return self._compile_dict(schema)
        elif isinstance(schema, list):
            return self._compile_list(schema)
        elif isinstance(schema, tuple):
            return self._compile_tuple(schema)
        type_ = type(schema)
        if type_ is type:
            type_ = schema
        if type_ in (int, long, str, unicode, float, complex, object,
                     list, dict, type(None)) or callable(schema):
            return _compile_scalar(schema)
        raise SchemaError('unsupported schema data type %r' %
                          type(schema).__name__)

    def _compile_mapping(self, schema, invalid_msg=None):
        """Create validator for given mapping."""
        invalid_msg = ' ' + (invalid_msg or 'for mapping value')
        default_required_keys = set(key for key in schema
                                    if
                                    (self.required and not isinstance(key, Optional))
                                    or
                                    isinstance(key, Required))

        _compiled_schema = {}
        for skey, svalue in schema.iteritems():
            new_key = self._compile(skey)
            new_value = self._compile(svalue)
            _compiled_schema[skey] = (new_key, new_value)

        def validate_mapping(path, iterable, out):
            required_keys = default_required_keys.copy()
            error = None
            errors = []
            for key, value in iterable:
                key_path = path + [key]
                for skey, (ckey, cvalue) in _compiled_schema.iteritems():
                    try:
                        new_key = ckey(key_path, key)
                    except Invalid as e:
                        if len(e.path) > len(key_path):
                            raise
                        if not error or len(e.path) > len(error.path):
                            error = e
                        continue
                    # Backtracking is not performed once a key is selected, so if
                    # the value is invalid we immediately throw an exception.
                    try:
                        out[new_key] = cvalue(key_path, value)
                    except Invalid as e:
                        if len(e.path) > len(key_path):
                            errors.append(e)
                        else:
                            errors.append(Invalid(e.msg + invalid_msg, e.path))
                        break

                    # Key and value okay, mark any Required() fields as found.
                    required_keys.discard(skey)
                    break
                else:
                    if self.extra:
                        out[key] = value
                    else:
                        errors.append(Invalid('extra keys not allowed', key_path))
            for key in required_keys:
                msg = key.msg if hasattr(key, 'msg') and key.msg else 'required key not provided'
                errors.append(Invalid(msg, path + [key]))
            if errors:
                raise MultipleInvalid(errors)
            return out

        return validate_mapping

    def _compile_object(self, schema):
        """Validate an object.

        Has the same behavior as dictionary validator but work with object
        attributes.

        For example:

            >>> class Structure(object):
            ...     def __init__(self, one=None, three=None):
            ...         self.one = one
            ...         self.three = three
            ...
            >>> validate = Schema(Object({'one': 'two', 'three': 'four'}, cls=Structure))
            >>> validate(Structure(one='three'))
            Traceback (most recent call last):
            ...
            MultipleInvalid: not a valid value for object value @ data['one']

        """
        base_validate = self._compile_mapping(schema,
            invalid_msg='for object value')

        def validate_object(path, data):
            if schema.cls is not UNDEFINED and not isinstance(data, schema.cls):
                raise Invalid('expected a {0!r}'.format(schema.cls), path)
            iterable = _iterate_object(data)
            iterable = ifilter(lambda item: item[1] is not None, iterable)
            out = base_validate(path, iterable, {})
            return type(data)(**out)

        return validate_object

    def _compile_dict(self, schema):
        """Validate a dictionary.

        A dictionary schema can contain a set of values, or at most one
        validator function/type.

        A dictionary schema will only validate a dictionary:

            >>> validate = Schema({})
            >>> validate([])
            Traceback (most recent call last):
            ...
            MultipleInvalid: expected a dictionary

        An invalid dictionary value:

            >>> validate = Schema({'one': 'two', 'three': 'four'})
            >>> validate({'one': 'three'})
            Traceback (most recent call last):
            ...
            MultipleInvalid: not a valid value for dictionary value @ data['one']

        An invalid key:

            >>> validate({'two': 'three'})
            Traceback (most recent call last):
            ...
            MultipleInvalid: extra keys not allowed @ data['two']

        Validation function, in this case the "int" type:

            >>> validate = Schema({'one': 'two', 'three': 'four', int: str})

        Valid integer input:

            >>> validate({10: 'twenty'})
            {10: 'twenty'}

        By default, a "type" in the schema (in this case "int") will be used
        purely to validate that the corresponding value is of that type. It
        will not Coerce the value:

            >>> validate({'10': 'twenty'})
            Traceback (most recent call last):
            ...
            MultipleInvalid: extra keys not allowed @ data['10']

        Wrap them in the Coerce() function to achieve this:

            >>> validate = Schema({'one': 'two', 'three': 'four',
            ...                    Coerce(int): str})
            >>> validate({'10': 'twenty'})
            {10: 'twenty'}

        Custom message for required key

            >>> validate = Schema({Required('one', 'required'): 'two'})
            >>> validate({})
            Traceback (most recent call last):
            ...
            MultipleInvalid: required @ data['one']

        (This is to avoid unexpected surprises.)
        """
        base_validate = self._compile_mapping(schema,
            invalid_msg='for dictionary value')

        def validate_dict(path, data):
            if not isinstance(data, dict):
                raise Invalid('expected a dictionary', path)

            out = type(data)()
            return base_validate(path, data.iteritems(), out)

        return validate_dict

    def _compile_sequence(self, schema, seq_type):
        """Validate a sequence type.

        This is a sequence of valid values or validators tried in order.

        >>> validator = Schema(['one', 'two', int])
        >>> validator(['one'])
        ['one']
        >>> validator([3.5])
        Traceback (most recent call last):
        ...
        MultipleInvalid: invalid list value @ data[0]
        >>> validator([1])
        [1]
        """
        _compiled = [self._compile(s) for s in schema]
        seq_type_name = seq_type.__name__

        def validate_sequence(path, data):
            if not isinstance(data, seq_type):
                raise Invalid('expected a %s' % seq_type_name, path)

            # Empty seq schema, allow any data.
            if not schema:
                return data

            out = []
            invalid = None
            errors = []
            index_path = UNDEFINED
            for i, value in enumerate(data):
                index_path = path + [i]
                invalid = None
                for validate in _compiled:
                    try:
                        out.append(validate(index_path, value))
                        break
                    except Invalid as e:
                        if len(e.path) > len(index_path):
                            raise
                        invalid = e
                else:
                    if len(invalid.path) <= len(index_path):
                        invalid = Invalid('invalid %s value' % seq_type_name, index_path)
                    errors.append(invalid)
            if errors:
                raise MultipleInvalid(errors)
            return type(data)(out)
        return validate_sequence

    def _compile_tuple(self, schema):
        """Validate a tuple.

        A tuple is a sequence of valid values or validators tried in order.

        >>> validator = Schema(('one', 'two', int))
        >>> validator(('one',))
        ('one',)
        >>> validator((3.5,))
        Traceback (most recent call last):
        ...
        MultipleInvalid: invalid tuple value @ data[0]
        >>> validator((1,))
        (1,)
        """
        return self._compile_sequence(schema, tuple)

    def _compile_list(self, schema):
        """Validate a list.

        A list is a sequence of valid values or validators tried in order.

        >>> validator = Schema(['one', 'two', int])
        >>> validator(['one'])
        ['one']
        >>> validator([3.5])
        Traceback (most recent call last):
        ...
        MultipleInvalid: invalid list value @ data[0]
        >>> validator([1])
        [1]
        """
        return self._compile_sequence(schema, list)


def _compile_scalar(schema):
    """A scalar value.

    The schema can either be a value or a type.

    >>> _compile_scalar(int)([], 1)
    1
    >>> _compile_scalar(float)([], '1')
    Traceback (most recent call last):
    ...
    Invalid: expected float

    Callables have
    >>> _compile_scalar(lambda v: float(v))([], '1')
    1.0

    As a convenience, ValueError's are trapped:

    >>> _compile_scalar(lambda v: float(v))([], 'a')
    Traceback (most recent call last):
    ...
    Invalid: not a valid value
    """
    if isinstance(schema, type):
        def validate_instance(path, data):
            if isinstance(data, schema):
                return data
            else:
                raise Invalid('expected %s' % schema.__name__, path)
        return validate_instance

    if callable(schema):
        def validate_callable(path, data):
            try:
                return schema(data)
            except ValueError as e:
                raise Invalid('not a valid value', path)
            except Invalid as e:
                raise Invalid(e.msg, path + e.path)
        return validate_callable

    def validate_value(path, data):
        if data != schema:
            raise Invalid('not a valid value', path)
        return data

    return validate_value


def _iterate_object(obj):
    """Return iterator over object attributes. Respect objects with
    defined __slots__.

    """
    d = {}
    try:
        d = vars(obj)
    except TypeError:
        # maybe we have named tuple here?
        if hasattr(obj, '_asdict'):
            d = obj._asdict()
    for item in d.iteritems():
        yield item
    try:
        slots = obj.__slots__
    except AttributeError:
        pass
    else:
        for key in slots:
            if key != '__dict__':
                yield (key, getattr(obj, key))
    raise StopIteration()


class Object(dict):
    """Indicate that we should work with attributes, not keys."""

    def __init__(self, schema, cls=UNDEFINED):
        self.cls = cls
        super(Object, self).__init__(schema)


class Marker(object):
    """Mark nodes for special treatment."""

    def __init__(self, schema, msg=None):
        self.schema = schema
        self._schema = Schema(schema)
        self.msg = msg

    def __call__(self, v):
        try:
            return self._schema(v)
        except Invalid as e:
            if not self.msg or len(e.path) > 1:
                raise
            raise Invalid(self.msg)

    def __str__(self):
        return str(self.schema)

    def __repr__(self):
        return repr(self.schema)


class Optional(Marker):
    """Mark a node in the schema as optional."""


class Required(Marker):
    """Mark a node in the schema as being required."""


def Extra(_):
    """Allow keys in the data that are not present in the schema."""
    raise SchemaError('"Extra" should never be called')


# As extra() is never called there's no way to catch references to the
# deprecated object, so we just leave an alias here instead.
extra = Extra


def Msg(schema, msg):
    """Report a user-friendly message if a schema fails to validate.

    >>> validate = Schema(
    ...   Msg(['one', 'two', int],
    ...       'should be one of "one", "two" or an integer'))
    >>> validate(['three'])
    Traceback (most recent call last):
    ...
    MultipleInvalid: should be one of "one", "two" or an integer

    Messages are only applied to invalid direct descendants of the schema:

    >>> validate = Schema(Msg([['one', 'two', int]], 'not okay!'))
    >>> validate([['three']])
    Traceback (most recent call last):
    ...
    MultipleInvalid: invalid list value @ data[0][0]
    """
    schema = Schema(schema)

    @wraps(Msg)
    def f(v):
        try:
            return schema(v)
        except Invalid as e:
            if len(e.path) > 1:
                raise e
            else:
                raise Invalid(msg)
    return f


def message(default=None):
    """Convenience decorator to allow functions to provide a message.

    Set a default message:

        >>> @message('not an integer')
        ... def isint(v):
        ...   return int(v)

        >>> validate = Schema(isint())
        >>> validate('a')
        Traceback (most recent call last):
        ...
        MultipleInvalid: not an integer

    The message can be overridden on a per validator basis:

        >>> validate = Schema(isint('bad'))
        >>> validate('a')
        Traceback (most recent call last):
        ...
        MultipleInvalid: bad
    """
    def decorator(f):
        @wraps(f)
        def check(msg=None):
            @wraps(f)
            def wrapper(*args, **kwargs):
                try:
                    return f(*args, **kwargs)
                except ValueError:
                    raise Invalid(msg or default or 'invalid value')
            return wrapper
        return check
    return decorator


def truth(f):
    """Convenience decorator to convert truth functions into validators.

        >>> @truth
        ... def isdir(v):
        ...   return os.path.isdir(v)
        >>> validate = Schema(isdir)
        >>> validate('/')
        '/'
        >>> validate('/notavaliddir')
        Traceback (most recent call last):
        ...
        MultipleInvalid: not a valid value
    """
    @wraps(f)
    def check(v):
        t = f(v)
        if not t:
            raise ValueError
        return v
    return check


def Coerce(type, msg=None):
    """Coerce a value to a type.

    If the type constructor throws a ValueError, the value will be marked as
    Invalid.
    """
    @wraps(Coerce)
    def f(v):
        try:
            return type(v)
        except ValueError:
            raise Invalid(msg or ('expected %s' % type.__name__))
    return f


@message('value was not true')
@truth
def IsTrue(v):
    """Assert that a value is true, in the Python sense.

    >>> validate = Schema(IsTrue())

    "In the Python sense" means that implicitly false values, such as empty
    lists, dictionaries, etc. are treated as "false":

    >>> validate([])
    Traceback (most recent call last):
    ...
    MultipleInvalid: value was not true
    >>> validate([1])
    [1]
    >>> validate(False)
    Traceback (most recent call last):
    ...
    MultipleInvalid: value was not true

    ...and so on.
    """
    return v


@message('value was not false')
def IsFalse(v):
    """Assert that a value is false, in the Python sense.

    (see :func:`IsTrue` for more detail)

    >>> validate = Schema(IsFalse())
    >>> validate([])
    []
    """
    if v:
        raise ValueError
    return v


@message('expected boolean')
def Boolean(v):
    """Convert human-readable boolean values to a bool.

    Accepted values are 1, true, yes, on, enable, and their negatives.
    Non-string values are cast to bool.

    >>> validate = Schema(Boolean())
    >>> validate(True)
    True
    >>> validate('moo')
    Traceback (most recent call last):
    ...
    MultipleInvalid: expected boolean
    """
    if isinstance(v, basestring):
        v = v.lower()
        if v in ('1', 'true', 'yes', 'on', 'enable'):
            return True
        if v in ('0', 'false', 'no', 'off', 'disable'):
            return False
        raise ValueError
    return bool(v)


def Any(*validators, **kwargs):
    """Use the first validated value.

    :param msg: Message to deliver to user if validation fails.
    :returns: Return value of the first validator that passes.

    >>> validate = Schema(Any('true', 'false',
    ...                       All(Any(int, bool), Coerce(bool))))
    >>> validate('true')
    'true'
    >>> validate(1)
    True
    >>> validate('moo')
    Traceback (most recent call last):
    ...
    MultipleInvalid: no valid value found
    """
    msg = kwargs.pop('msg', None)
    schemas = [Schema(val) for val in validators]

    @wraps(Any)
    def f(v):
        for schema in schemas:
            try:
                return schema(v)
            except Invalid as e:
                if len(e.path) > 1:
                    raise
                pass
        else:
            raise Invalid(msg or 'no valid value found')
    return f


def All(*validators, **kwargs):
    """Value must pass all validators.

    The output of each validator is passed as input to the next.

    :param msg: Message to deliver to user if validation fails.

    >>> validate = Schema(All('10', Coerce(int)))
    >>> validate('10')
    10
    """
    msg = kwargs.pop('msg', None)
    schemas = [Schema(val) for val in validators]

    def f(v):
        try:
            for schema in schemas:
                v = schema(v)
        except Invalid as e:
            raise e if msg is None else Invalid(msg)
        return v
    return f


def Match(pattern, msg=None):
    """Value must be a string that matches the regular expression.

    >>> validate = Schema(Match(r'^0x[A-F0-9]+$'))
    >>> validate('0x123EF4')
    '0x123EF4'
    >>> validate('123EF4')
    Traceback (most recent call last):
    ...
    MultipleInvalid: does not match regular expression

    >>> validate(123)
    Traceback (most recent call last):
    ...
    MultipleInvalid: expected string or buffer

    Pattern may also be a _compiled regular expression:

    >>> validate = Schema(Match(re.compile(r'0x[A-F0-9]+', re.I)))
    >>> validate('0x123ef4')
    '0x123ef4'
    """
    if isinstance(pattern, basestring):
        pattern = re.compile(pattern)

    def f(v):
        try:
            match = pattern.match(v)
        except TypeError:
            raise Invalid("expected string or buffer")
        if not match:
            raise Invalid(msg or 'does not match regular expression')
        return v
    return f


def Replace(pattern, substitution, msg=None):
    """Regex substitution.

    >>> validate = Schema(All(Replace('you', 'I'),
    ...                       Replace('hello', 'goodbye')))
    >>> validate('you say hello')
    'I say goodbye'
    """
    if isinstance(pattern, basestring):
        pattern = re.compile(pattern)

    def f(v):
        return pattern.sub(substitution, v)
    return f


@message('expected a URL')
def Url(v):
    """Verify that the value is a URL."""
    try:
        urlparse.urlparse(v)
        return v
    except:
        raise ValueError


@message('not a file')
@truth
def IsFile(v):
    """Verify the file exists."""
    return os.path.isfile(v)


@message('not a directory')
@truth
def IsDir(v):
    """Verify the directory exists.

    >>> IsDir()('/')
    '/'
    """
    return os.path.isdir(v)


@message('path does not exist')
@truth
def PathExists(v):
    """Verify the path exists, regardless of its type."""
    return os.path.exists(v)


def Range(min=None, max=None, msg=None):
    """Limit a value to a range.

    Either min or max may be omitted.

    :raises Invalid: If the value is outside the range.
    """
    @wraps(Range)
    def f(v):
        if min is not None and v < min:
            raise Invalid(msg or 'value must be at least %s' % min)
        if max is not None and v > max:
            raise Invalid(msg or 'value must be at most %s' % max)
        return v
    return f


def Clamp(min=None, max=None, msg=None):
    """Clamp a value to a range.

    Either min or max may be omitted.
    """
    @wraps(Clamp)
    def f(v):
        if min is not None and v < min:
            v = min
        if max is not None and v > max:
            v = max
        return v
    return f


def Length(min=None, max=None, msg=None):
    """The length of a value must be in a certain range."""
    @wraps(Length)
    def f(v):
        if min is not None and len(v) < min:
            raise Invalid(msg or 'length of value must be at least %s' % min)
        if max is not None and len(v) > max:
            raise Invalid(msg or 'length of value must be at most %s' % max)
        return v
    return f


def Lower(v):
    """Transform a string to lower case.

    >>> s = Schema(Lower)
    >>> s('HI')
    'hi'
    """
    return str(v).lower()


def Upper(v):
    """Transform a string to upper case.

    >>> s = Schema(Upper)
    >>> s('hi')
    'HI'
    """
    return str(v).upper()


def Capitalize(v):
    """Capitalise a string.

    >>> s = Schema(Capitalize)
    >>> s('hello world')
    'Hello world'
    """
    return str(v).capitalize()


def Title(v):
    """Title case a string.

    >>> s = Schema(Title)
    >>> s('hello world')
    'Hello World'
    """
    return str(v).title()


def DefaultTo(default_value, msg=None):
    """Sets a value to default_value if none provided.

    >>> s = Schema(DefaultTo(42))
    >>> s(None)
    42
    """
    @wraps(DefaultTo)
    def f(v):
        if v is None:
            v = default_value
        return v
    return f


if __name__ == '__main__':
    import doctest
    doctest.testmod()
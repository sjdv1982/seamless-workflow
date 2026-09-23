"""Pure configuration validation and producer-side semantic fingerprinting."""
import copy
from seamless import Buffer, Checksum
from .graph import TransformerConfig


def update_config(original, field, value, key=None, delete=False):
    cfg = copy.deepcopy(original)
    compiled = isinstance(cfg, TransformerConfig) and cfg.compilation is not None
    if compiled and field in ('optional_pin', 'optional_pins') and value:
        raise TypeError('compiled inputs cannot be optional')
    if compiled and field == 'schema':
        import yaml
        from seamless_signature import Signature, generate_header
        from seamless_transformer.compiled_validation import validate_declarations
        sig = Signature.from_dict(yaml.safe_load(value))
        header = generate_header(sig)
        cfg.schema, cfg.header = value, header
        allowed_metavars = {f'max{w}' for w in sig.output_wildcards}
        cfg.meta['metavars'] = {k: v for k, v in cfg.meta.get('metavars', {}).items()
                                if k in allowed_metavars}
        cfg.pins = {p.name for p in sig.inputs}
        cfg.celltypes = {**{p: cfg.celltypes.get(p, 'mixed') for p in cfg.pins},
                         'result': cfg.celltypes.get('result', 'mixed')}
        validate_declarations(sig, cfg.celltypes, warn=True)
        return cfg
    if field == 'optional_pin':
        import inspect
        from seamless_transformer.optional_pins import optional_names
        signature = inspect.signature(cfg.callable) if callable(cfg.callable) else None
        if cfg.language != 'python' or key not in optional_names(signature):
            raise AttributeError(key)
        if value:
            cfg.optional_pins.add(key)
        else:
            cfg.optional_pins.discard(key)
    elif field in {'celltypes', 'modules', 'globals'}:
        mapping = getattr(cfg, field)
        if delete:
            mapping.pop(key, None)
        else:
            if field == 'celltypes':
                from seamless.checksum.celltypes import celltypes
                value = {int:'int', float:'float', str:'str', bool:'bool', bytes:'bytes'}.get(value, value)
                if value not in celltypes + ['deepcell','deepfolder','folder','module']:
                    raise TypeError(f'Unknown celltype: {value!r}')
                if compiled and key != 'result':
                    from seamless_transformer.compiled_validation import ALLOWED_CELLTYPES, CompiledPinCelltypeError
                    if key not in cfg.pins:
                        raise AttributeError(key)
                    if value not in ALLOWED_CELLTYPES:
                        raise CompiledPinCelltypeError(f"Compiled pin {key!r}: unsupported celltype {value!r}")
                cfg.check_pin_name(key, allow_result=True)
                if key != 'result': cfg.pins.add(key)
            mapping[key] = copy.deepcopy(value)
    elif field in {'driver', 'allow_input_fingertip'}:
        cfg.meta[field] = bool(value)
    elif field == 'meta':
        cfg.meta.update(copy.deepcopy(value))
    else:
        if field not in cfg.__dataclass_fields__ or field in {'config_token', 'callable'}:
            raise AttributeError(field)
        if field == 'optional_pins': value = set(value or ())
        if field in {'scratch','direct_print'}: value = bool(value)
        if field == 'language' and value is None: value = 'python'
        if field == 'local': cfg.meta['local'] = value
        if field == 'celltype':
            if value is None: raise TypeError('celltype must name a supported type')
            value = {int:'int', float:'float', str:'str', bool:'bool', bytes:'bytes'}.get(value, value)
            Buffer._map_celltype(value)
        setattr(cfg, field, copy.deepcopy(value))
    if compiled and field == 'celltypes' and cfg.schema:
        import yaml
        from seamless_signature import Signature
        from seamless_transformer.compiled_validation import validate_declarations
        try:
            sig = Signature.from_dict(yaml.safe_load(cfg.schema))
        except Exception:
            pass
        else:
            validate_declarations(sig, cfg.celltypes, warn=True)
    return cfg


def _plain(value):
    if isinstance(value, Checksum): return {'checksum':value.hex()}
    if isinstance(value, Buffer): return {'buffer':value.get_checksum().hex()}
    if isinstance(value, dict): return {k:_plain(v) for k,v in value.items()}
    if isinstance(value, (tuple,list)): return [_plain(v) for v in value]
    if isinstance(value, (set,frozenset)): return sorted(value)
    return value


def fingerprint(cfg):
    if not isinstance(cfg, TransformerConfig): return
    fields = ('language','celltypes','optional_pins','modules','globals','meta','environment',
              'scratch','local','direct_print','schema','compilation','objects','header')
    # Code and input checksums are separate components of runtime demand.
    cfg.config_token = Buffer(_plain({k:getattr(cfg,k) for k in fields}), 'mixed').get_checksum().hex()

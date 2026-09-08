"""Pure configuration validation and producer-side semantic fingerprinting."""
import copy
from seamless import Buffer, Checksum
from .graph import TransformerConfig


def update_config(original, field, value, key=None, delete=False):
    cfg = copy.deepcopy(original)
    if field in {'celltypes', 'modules', 'globals'}:
        mapping = getattr(cfg, field)
        if delete:
            mapping.pop(key, None)
        else:
            if field == 'celltypes':
                from seamless.checksum.celltypes import celltypes
                value = {int:'int', float:'float', str:'str', bool:'bool', bytes:'bytes'}.get(value, value)
                if value not in celltypes + ['deepcell','deepfolder','folder','module']:
                    raise TypeError(f'Unknown celltype: {value!r}')
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
        if field in {'celltype','target_celltype'}:
            if value is None and field == 'target_celltype': value = cfg.celltype
            Buffer._map_celltype(value)
        setattr(cfg, field, copy.deepcopy(value))
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

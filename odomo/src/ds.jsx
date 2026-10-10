// Provenance: copied from omodachi-web `src/app/ds.jsx` at main 0da28c9 (same author,
// MIT, like this repository). Kept in step by hand; the site's own build, which
// makes no requests and stores nothing, stays separate and untouched.
//
import React, {useEffect, useState} from 'react';
import Icon from './Icon.jsx';

// The App's primitives, one for one with omodachi-ios
// `Omodachi/DesignSystem/Primitives/`: PanelArea, PanelTitle, GroupLabel, Row,
// SettingLine (with ValueLine, SettingChoice, SettingAction), FlowButton,
// SquareToggle, SegmentedRow, StateChip, InlineConfirm, EmptyState, KeyBlock, Field and PinButton. The numbers are the App's too:
// a 44 header and hit area, 50 and 58 rows, 28 controls, a 1px control border
// and a 2px panel rule, zero radius.

// PanelArea.swift. `read` is "read and choose": a scrolling column no wider
// than 760, centred. `work` fills the area.
export function PanelArea({title, accessory, shape = 'read', testid, label, children}) {
  return (
    <section className={'parea parea--' + shape} data-testid={testid} aria-label={label}>
      <header className="parea__head">
        {typeof title === 'string' ? <PanelTitle>{title}</PanelTitle> : title}
        <span className="grow" />
        {accessory || null}
      </header>
      {shape === 'read' ? (
        <div className="parea__scroll">
          <div className="parea__col">{children}</div>
        </div>
      ) : (
        <div className="parea__work">{children}</div>
      )}
    </section>
  );
}

export function PanelTitle({children}) {
  return <h2 className="ptitle">{children}</h2>;
}

// GroupLabel: body-small, tracked, secondary, on a 44 line.
export function GroupLabel({children, action}) {
  return (
    <div className="glabel">
      <span>{children}</span>
      {action || null}
    </div>
  );
}

// Row.swift. 50 high with one line, 58 with a detail; a 36 glyph box; one
// hairline per level of depth, 17 apart.
export function Row({glyph, title, detail, disabled, selected, depth = 0, trailing, onClick, testid, ...rest}) {
  const rules = [];
  for (let i = 0; i < depth; i++) rules.push(<i key={i} className="row__rule" />);
  return (
    <div className={'row' + (detail ? ' row--detail' : '')} data-testid={testid}>
      {rules}
      <button
        type="button"
        className={'row__tap' + (selected ? ' sel' : '')}
        disabled={disabled || undefined}
        onClick={onClick}
        {...rest}
      >
        {glyph ? (
          <span className="row__gl">
            {typeof glyph === 'string' ? <Icon name={glyph} size={18} /> : glyph}
          </span>
        ) : null}
        <span className="row__tx">
          <span className="row__t1">{title}</span>
          {detail ? <span className="row__t2">{detail}</span> : null}
        </span>
      </button>
      {trailing || null}
    </div>
  );
}

// ODOMO-SETTINGS-1: SettingLine.swift (SETTINGS-LAYOUT-1). One shape for every
// Settings row, and the row says which arrangement it wants - never the device:
//   beside   a switch, a short status, a single action: title left, the thing
//            right, at every width;
//   fitting  a choice or a value of no fixed length: beside the title when it
//            fits there at its natural width, else under it, left-aligned
//            (CSS flex-wrap measures it, so the column decides);
//   under    a long value (an id, a URL): always under, wrapped, selectable.
// The detail line always sits under the title and wraps in the title's column;
// it never widens the row (`contain: inline-size`), so a long explanation can
// never push a control under its title. One title size, one padding.
export function SettingTitle({title, detail, testid}) {
  return (
    <span className="sline__tx">
      <span className="sline__t" data-testid={testid ? testid + '-title' : undefined}>{title}</span>
      {detail ? <span className="sline__d">{detail}</span> : null}
    </span>
  );
}

export function SettingLine({title, detail, arrange = 'beside', glyph, testid, titleTestid, choice = false, children}) {
  return (
    <div className={'sline sline--' + arrange + (choice ? ' sline--choice' : '')} data-testid={testid} data-arrange={arrange}>
      {glyph ? <span className="sline__gl"><Icon name={glyph} size={18} /></span> : null}
      <SettingTitle title={title} detail={detail} testid={titleTestid || testid} />
      {children != null ? <span className="sline__end">{children}</span> : null}
    </div>
  );
}

// A name and what it is set to (SettingValue): `fitting` for a short value,
// `under` for an id or a URL. Never cut: it wraps, and it can be selected.
export function ValueLine({name, value, testid, arrange = 'fitting'}) {
  return (
    <SettingLine title={name} arrange={arrange} testid={testid}>
      <span className="sline__v">{value}</span>
    </SettingLine>
  );
}

// A choice (SegmentedRow) with its title, `fitting`. Under its title it
// stretches to at most 420, as the App's does.
export function SettingChoice({title, detail, testid, rowTestid, ...segmented}) {
  return (
    <SettingLine title={title} detail={detail} arrange="fitting" choice testid={rowTestid} titleTestid={testid}>
      <Segmented label={title} testid={testid} {...segmented} />
    </SettingLine>
  );
}

// A single action (SettingAction): the row is the button or the link, the
// chevron on the right says where it goes.
export function SettingAction({title, detail, glyph, trailing = 'right', href, testid, onClick, ...rest}) {
  const body = (
    <>
      {glyph ? <span className="sline__gl"><Icon name={glyph} size={18} /></span> : null}
      <SettingTitle title={title} detail={detail} />
      <span className="sline__end sline__cv"><Icon name={trailing} size={14} /></span>
    </>
  );
  return href ? (
    <a className="sline sline--beside sline--tap" data-arrange="beside" href={href} data-testid={testid} {...rest}>{body}</a>
  ) : (
    <button type="button" className="sline sline--beside sline--tap" data-arrange="beside" data-testid={testid}
      onClick={onClick} {...rest}>{body}</button>
  );
}

// FlowButton.swift: primary is the accent, secondary the bordered control,
// ghost a bordered control with dimmer text.
export function FlowButton({kind = 'primary', children, onClick, disabled, ...rest}) {
  return (
    <button type="button" className={'fbtn fbtn--' + kind} onClick={onClick} disabled={disabled || undefined} {...rest}>
      {children}
    </button>
  );
}

// SquareToggle.swift: a square track and a square knob, never a capsule.
export function SquareToggle({on, label, onChange, disabled, testid}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      className={'tog' + (on ? ' on' : '')}
      disabled={disabled || undefined}
      data-testid={testid}
      onClick={() => onChange(!on)}
    >
      <i />
    </button>
  );
}

// A switch: always beside its title (SettingLine `beside`).
export function SquareToggleRow({title, detail, glyph, on, onChange, disabled, label, testid, rowTestid}) {
  return (
    <SettingLine title={title} detail={detail} glyph={glyph} testid={rowTestid} titleTestid={testid}>
      <SquareToggle on={on} label={label || title} onChange={onChange} disabled={disabled} testid={testid} />
    </SettingLine>
  );
}

// SegmentedRow: one bordered control, the chosen option filled and accented.
export function Segmented({options, value, onChange, label, testid}) {
  return (
    <div className="segrow" role="radiogroup" aria-label={label} data-testid={testid}>
      {options.map(([text, v]) => (
        <button
          key={String(v)}
          type="button"
          role="radio"
          aria-checked={v === value}
          className={v === value ? 'on' : ''}
          onClick={() => onChange(v)}
        >
          {text}
        </button>
      ))}
    </div>
  );
}

export function StateChip({text, kind = 'muted'}) {
  return <span className={'schip schip--' + kind}>{text}</span>;
}

// InlineConfirm.swift: the first press arms the control and says what the
// second press does; it disarms itself after three seconds.
export function InlineConfirm({title, confirmTitle, onConfirm, kind = 'secondary', testid}) {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return undefined;
    const t = setTimeout(() => setArmed(false), 3000);
    return () => clearTimeout(t);
  }, [armed]);
  return (
    <FlowButton
      kind={armed ? 'danger' : kind}
      data-testid={testid}
      data-armed={armed ? '1' : '0'}
      onClick={() => {
        if (armed) {
          setArmed(false);
          onConfirm();
        } else setArmed(true);
      }}
    >
      {armed ? confirmTitle : title}
    </FlowButton>
  );
}

export function EmptyState({title, detail, testid}) {
  return (
    <div className="estate" data-testid={testid}>
      <span className="estate__t">{title}</span>
      {detail ? <span className="estate__d">{detail}</span> : null}
    </div>
  );
}

// KeybindingsView.KeyBlock: one bordered cap per key.
export function KeyBlock({display}) {
  const parts = display
    .split('+')
    .flatMap((p) => p.trim().split(' '))
    .filter(Boolean);
  return (
    <span className="keys" aria-hidden="true">
      {parts.map((k, i) => (
        <kbd key={i}>{k}</kbd>
      ))}
    </span>
  );
}

// Field.swift: a search field on a 44 line with a hairline under it.
export function Field({placeholder, value, onChange, label, onClear, clearLabel, testid}) {
  return (
    <div className="field">
      <Icon name="search" size={16} />
      <input
        type="search"
        value={value}
        placeholder={placeholder}
        aria-label={label || placeholder}
        data-testid={testid}
        onChange={(e) => onChange(e.target.value)}
      />
      {value && onClear ? (
        <button type="button" className="field__clear" aria-label={clearLabel} onClick={onClear}>
          <Icon name="close" size={14} />
        </button>
      ) : null}
    </div>
  );
}

export function PinButton({pinned, label, onClick}) {
  return (
    <button type="button" className={'pinbtn' + (pinned ? ' on' : '')} aria-label={label} aria-pressed={pinned} onClick={onClick}>
      <Icon name="pin" size={15} />
    </button>
  );
}

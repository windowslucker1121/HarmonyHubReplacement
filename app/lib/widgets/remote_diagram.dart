/// A vector illustration of the physical Harmony Companion remote.
///
/// Drawn from the real button layout in `buttons.json` rather than a photo:
/// every key gets an exact, hand-placed region on a fixed design canvas, so
/// press highlights land precisely on the button that fired instead of on an
/// approximation of where a product photo's buttons might be.
library;

import 'package:flutter/material.dart';

import '../api/models.dart';

/// Design-space size of the remote illustration. All button rects below are
/// specified in this coordinate system and scaled to fit at paint time.
const double kRemoteWidth = 340;
const double kRemoteHeight = 1150;

enum _Shape { rounded, circle, dot }

class _KeySpec {
  const _KeySpec(
    this.key,
    this.rect, {
    this.shape = _Shape.rounded,
    this.icon,
    this.text,
    this.color,
  });

  final String key;
  final Rect rect;
  final _Shape shape;
  final IconData? icon;
  final String? text;
  final Color? color;
}

// A strict 3-column grid, one row per physical row of the remote (a 4-column
// grid for the single row of colour keys), matching the reference photo
// rather than an idealised D-pad cluster -- every button lives at the same
// row/column position it does on the real remote.
const double _kMarginX = 20;
const double _kColW = 90;
const double _kColGap = 15;
const double _kRowH = 46;
const double _kRowGap = 10;
const double _kRowPitch = _kRowH + _kRowGap;
const double _kTopMargin = 30;

// Extra breathing room between button groups, on top of the normal row gap:
// activities -> SmartHome -> colour keys -> DVR/Guide/Info -> Exit/Menu, and
// separately Mute/Back -> transport -> numeric keypad.
const double _kExtraGap = 24;
const Set<int> _kExtraGapBeforeRow = {2, 4, 5, 6, 11, 13};

double _colX(int col) => _kMarginX + col * (_kColW + _kColGap);
double _rowY(int row) {
  final extraGaps = _kExtraGapBeforeRow.where((r) => r <= row).length;
  return _kTopMargin + row * _kRowPitch + extraGaps * _kExtraGap;
}

/// A cell in the 3-column grid: row 0 is the topmost row.
Rect _cell(int row, int col) => Rect.fromLTWH(_colX(col), _rowY(row), _kColW, _kRowH);

const double _kColW4 = 60;
const double _kColGap4 = 20;
double _colX4(int col) => _kMarginX + col * (_kColW4 + _kColGap4);

/// A cell in the 4-column colour-key row.
Rect _cell4(int row, int col) => Rect.fromLTWH(_colX4(col), _rowY(row), _kColW4, _kRowH);

final List<_KeySpec> _kRemoteLayout = [
  // Row 0: Off.
  _KeySpec('consumer_0x01ec', _cell(0, 0), icon: Icons.power_settings_new),

  // Row 1: Music, TV, Movie.
  _KeySpec('consumer_0x01e8', _cell(1, 0), icon: Icons.music_note),
  _KeySpec('consumer_0x01ed', _cell(1, 1), icon: Icons.tv),
  _KeySpec('consumer_0x01e9', _cell(1, 2), icon: Icons.movie_outlined),

  // Row 2-3: SmartHome bulb / +- / plug, upper then lower.
  _KeySpec('consumer_0x0ff2', _cell(2, 0), icon: Icons.wb_incandescent_outlined),
  _KeySpec('consumer_0x0ff0', _cell(2, 1), icon: Icons.add),
  _KeySpec('consumer_0x0ff4', _cell(2, 2), icon: Icons.power),
  _KeySpec('consumer_0x0ff3', _cell(3, 0), icon: Icons.lightbulb_outline),
  _KeySpec('consumer_0x0ff1', _cell(3, 1), icon: Icons.remove),
  _KeySpec('consumer_0x0ff5', _cell(3, 2), icon: Icons.power_off_outlined),

  // Row 4: colour keys, 4 across.
  _KeySpec('colour_red', _cell4(4, 0), shape: _Shape.dot, color: const Color(0xFFE53935)),
  _KeySpec('colour_green', _cell4(4, 1), shape: _Shape.dot, color: const Color(0xFF43A047)),
  _KeySpec('colour_yellow', _cell4(4, 2), shape: _Shape.dot, color: const Color(0xFFFDD835)),
  _KeySpec('colour_blue', _cell4(4, 3), shape: _Shape.dot, color: const Color(0xFF1E88E5)),

  // Row 5: DVR, Guide, Info.
  _KeySpec('media_select_home', _cell(5, 0), icon: Icons.videocam_outlined),
  _KeySpec('program_guide', _cell(5, 1), icon: Icons.grid_view_rounded),
  _KeySpec('consumer_0x01ff', _cell(5, 2), icon: Icons.info_outline),

  // Row 6: Exit, (empty), Menu.
  _KeySpec('quit', _cell(6, 0), icon: Icons.close),
  _KeySpec('application_menu_key', _cell(6, 2), icon: Icons.menu),

  // Row 7-9: nav cluster as plain grid cells -- Volume/Dpad/Channel.
  _KeySpec('volume_up', _cell(7, 0), icon: Icons.volume_up),
  _KeySpec('up_arrow', _cell(7, 1), icon: Icons.keyboard_arrow_up),
  _KeySpec('channel_up', _cell(7, 2), icon: Icons.keyboard_double_arrow_up),
  _KeySpec('left_arrow', _cell(8, 0), icon: Icons.keyboard_arrow_left),
  _KeySpec('keypad_enter', _cell(8, 1), shape: _Shape.circle, text: 'OK'),
  _KeySpec('right_arrow', _cell(8, 2), icon: Icons.keyboard_arrow_right),
  _KeySpec('volume_down', _cell(9, 0), icon: Icons.volume_down),
  _KeySpec('down_arrow', _cell(9, 1), icon: Icons.keyboard_arrow_down),
  _KeySpec('channel_down', _cell(9, 2), icon: Icons.keyboard_double_arrow_down),

  // Row 10: Mute, (empty), Back.
  _KeySpec('mute', _cell(10, 0), icon: Icons.volume_off),
  _KeySpec('ac_back', _cell(10, 2), icon: Icons.arrow_back),

  // Row 11-12: transport controls.
  _KeySpec('rewind', _cell(11, 0), icon: Icons.fast_rewind),
  _KeySpec('play', _cell(11, 1), icon: Icons.play_arrow),
  _KeySpec('fast_forward', _cell(11, 2), icon: Icons.fast_forward),
  _KeySpec('record', _cell(12, 0), icon: Icons.fiber_manual_record),
  _KeySpec('pause', _cell(12, 1), icon: Icons.pause),
  _KeySpec('stop', _cell(12, 2), icon: Icons.stop),

  // Row 13-16: numeric keypad.
  _KeySpec('1', _cell(13, 0), text: '1'),
  _KeySpec('2', _cell(13, 1), text: '2'),
  _KeySpec('3', _cell(13, 2), text: '3'),
  _KeySpec('4', _cell(14, 0), text: '4'),
  _KeySpec('5', _cell(14, 1), text: '5'),
  _KeySpec('6', _cell(14, 2), text: '6'),
  _KeySpec('7', _cell(15, 0), text: '7'),
  _KeySpec('8', _cell(15, 1), text: '8'),
  _KeySpec('9', _cell(15, 2), text: '9'),
  _KeySpec('keypad', _cell(16, 0), text: '-'),
  _KeySpec('0', _cell(16, 1), text: '0'),
  _KeySpec('enter', _cell(16, 2), icon: Icons.keyboard_return),
];

/// Every button the illustration has a place for.
///
/// Screens that use the picture to choose a button need this: a key without a
/// slot is invisible here, and something has to offer it another way rather
/// than letting it become unreachable.
Set<String> get kRemoteLayoutKeys => {for (final spec in _kRemoteLayout) spec.key};

/// A small colour key, for legends above the diagram.
class RemoteLegendDot extends StatelessWidget {
  const RemoteLegendDot({super.key, required this.color, required this.label});

  final Color color;
  final String label;

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Container(
          width: 10,
          height: 10,
          decoration: BoxDecoration(color: color, shape: BoxShape.circle),
        ),
        const SizedBox(width: 6),
        Text(label, style: Theme.of(context).textTheme.bodySmall),
      ],
    );
  }
}

/// The remote picture, plus a way to reach any button it has no slot for.
///
/// Every screen that picks a button off the illustration wants both halves:
/// the picture, and a fallback so that a `buttons.json` carrying keys this
/// drawing does not know about cannot make them unreachable. Meant to be
/// placed inside an `Expanded`.
class RemoteBoard extends StatelessWidget {
  const RemoteBoard({
    super.key,
    required this.buttons,
    required this.status,
    required this.onTap,
    this.hint = 'Tap a button to send it, or hover to see its binding',
    this.emptyCaption = 'unbound',
  });

  final List<ButtonInfo> buttons;
  final RemoteKeyStatus Function(String key) status;

  /// Null makes the whole board read-only: the bindings still show, but no
  /// button can be pressed. That is the live view with the hub stopped --
  /// what each button *means* is configuration and still worth seeing, while
  /// sending it would reach nothing.
  final void Function(String key)? onTap;

  final String hint;
  final String emptyCaption;

  @override
  Widget build(BuildContext context) {
    final placed = kRemoteLayoutKeys;
    final unplaceable = buttons.where((b) => !placed.contains(b.key)).toList();

    return Column(
      children: [
        Expanded(
          child: Padding(
            padding: const EdgeInsets.fromLTRB(12, 8, 12, 4),
            child: RemoteDiagram(
              buttons: buttons,
              status: status,
              onTap: onTap,
              hint: hint,
              emptyCaption: emptyCaption,
            ),
          ),
        ),
        // Normally absent: the standard forty-eight all have a slot.
        if (unplaceable.isNotEmpty)
          SizedBox(
            height: 64,
            child: ListView(
              scrollDirection: Axis.horizontal,
              padding: const EdgeInsets.fromLTRB(16, 8, 16, 12),
              children: [
                for (final button in unplaceable)
                  Padding(
                    padding: const EdgeInsets.only(right: 8),
                    child: ActionChip(
                      avatar: Icon(
                        status(button.key).caption == null
                            ? Icons.radio_button_unchecked
                            : Icons.radio_button_checked,
                      ),
                      label: Text(button.label),
                      onPressed: onTap == null ? null : () => onTap!(button.key),
                    ),
                  ),
              ],
            ),
          ),
      ],
    );
  }
}

/// How one key should be drawn, and what to say about it.
///
/// Deliberately about appearance rather than meaning: the live view uses
/// [highlighted] for a key being pressed right now and the mapper uses it for
/// a key just assigned, and the diagram does not need to know the difference.
class RemoteKeyStatus {
  const RemoteKeyStatus({this.caption, this.highlighted = false, this.marked = false});

  /// What this key does. Null draws it as empty, and reads as such in the
  /// caption line -- the diagram's way of showing "nothing here yet".
  final String? caption;

  /// Drawn in the accent colour.
  final bool highlighted;

  /// Carries a corner dot: something about this key wants attention.
  final bool marked;

  static const RemoteKeyStatus empty = RemoteKeyStatus();
}

/// The remote illustration.
///
/// Renders every key from [buttons] that has a known layout slot (unknown
/// keys are silently skipped rather than crashing the view -- a custom
/// `buttons.json` with extra keys should still show the remote it does know).
class RemoteDiagram extends StatefulWidget {
  const RemoteDiagram({
    super.key,
    required this.buttons,
    required this.status,
    required this.onTap,
    this.hint = 'Tap a button to send it, or hover to see its binding',
    this.emptyCaption = 'unbound',
  });

  final List<ButtonInfo> buttons;
  final RemoteKeyStatus Function(String key) status;

  /// Null makes the diagram read-only; see [RemoteBoard.onTap].
  final void Function(String key)? onTap;

  /// Shown in the caption line while nothing is hovered.
  final String hint;

  /// How a key with no caption is described.
  final String emptyCaption;

  @override
  State<RemoteDiagram> createState() => _RemoteDiagramState();
}

class _RemoteDiagramState extends State<RemoteDiagram> {
  String? _highlighted;

  @override
  Widget build(BuildContext context) {
    final known = {for (final b in widget.buttons) b.key: b};
    final scheme = Theme.of(context).colorScheme;

    final caption = _highlighted != null && known.containsKey(_highlighted)
        ? _describe(known[_highlighted]!)
        : widget.hint;

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Expanded(
          child: Center(
            child: AspectRatio(
              aspectRatio: kRemoteWidth / kRemoteHeight,
              child: LayoutBuilder(
                builder: (context, constraints) {
                  final scale = constraints.maxWidth / kRemoteWidth;
                  return Stack(
                    children: [
                      Positioned.fill(
                        child: CustomPaint(painter: _RemoteBodyPainter(scheme: scheme)),
                      ),
                      for (final spec in _kRemoteLayout)
                        if (known.containsKey(spec.key))
                          _positioned(spec, scale, known[spec.key]!, scheme),
                    ],
                  );
                },
              ),
            ),
          ),
        ),
        const SizedBox(height: 8),
        Text(
          caption,
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          textAlign: TextAlign.center,
          style: Theme.of(context).textTheme.bodySmall?.copyWith(color: scheme.outline),
        ),
      ],
    );
  }

  /// The label + caption shown in the caption line and the tooltip, kept as
  /// one string so both agree and so widget tests can find a button by what
  /// it does rather than by on-screen text that may just be an icon.
  String _describe(ButtonInfo button) =>
      '${button.label} — ${widget.status(button.key).caption ?? widget.emptyCaption}';

  Widget _positioned(_KeySpec spec, double scale, ButtonInfo button, ColorScheme scheme) {
    final rect = Rect.fromLTWH(
      spec.rect.left * scale,
      spec.rect.top * scale,
      spec.rect.width * scale,
      spec.rect.height * scale,
    );
    final status = widget.status(spec.key);
    final flashing = status.highlighted;
    final bound = status.caption != null;

    final background = flashing
        ? scheme.primary
        : spec.shape == _Shape.dot
            ? (spec.color ?? scheme.surfaceContainerHighest).withValues(alpha: bound ? 1 : 0.55)
            : bound
                ? scheme.surfaceContainerHighest
                : scheme.surfaceContainerLow;
    final foreground = flashing
        ? scheme.onPrimary
        : spec.shape == _Shape.dot
            ? Colors.white
            : bound
                ? scheme.onSurface
                : scheme.outline;

    final shapeBorder = spec.shape == _Shape.circle || spec.shape == _Shape.dot
        ? const CircleBorder()
        : RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(10 * scale),
            side: BorderSide(color: flashing ? scheme.primary : scheme.outlineVariant, width: flashing ? 2 : 1),
          );

    return Positioned.fromRect(
      rect: rect,
      child: Tooltip(
        message: _describe(button),
        child: MouseRegion(
          onEnter: (_) => setState(() => _highlighted = spec.key),
          onExit: (_) => setState(() => _highlighted = null),
          child: Stack(
            clipBehavior: Clip.none,
            children: [
              Positioned.fill(
                child: Material(
                  color: background,
                  shape: shapeBorder,
                  child: InkWell(
                    customBorder: shapeBorder,
                    onTap: widget.onTap == null
                        ? null
                        : () {
                            setState(() => _highlighted = spec.key);
                            widget.onTap!(spec.key);
                          },
                    child: Center(
                      child: spec.icon != null
                          ? Icon(spec.icon, color: foreground, size: 20 * scale.clamp(0.6, 1.4))
                          : Text(
                              spec.text ?? '',
                              style: TextStyle(
                                color: foreground,
                                fontWeight: FontWeight.w600,
                                fontSize: 16 * scale.clamp(0.6, 1.4),
                              ),
                            ),
                    ),
                  ),
                ),
              ),
              // Sits over the corner rather than inside the key, so it does
              // not compete with the icon for a space this small.
              if (status.marked)
                Positioned(
                  top: -2,
                  right: -2,
                  child: IgnorePointer(
                    child: Container(
                      width: 10 * scale.clamp(0.7, 1.4),
                      height: 10 * scale.clamp(0.7, 1.4),
                      decoration: BoxDecoration(
                        color: scheme.tertiary,
                        shape: BoxShape.circle,
                        border: Border.all(color: scheme.surface, width: 1.5),
                      ),
                    ),
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}

/// The remote's plastic body: a rounded silhouette behind the buttons, plus
/// an IR window glyph at the top so it reads as a remote and not a keypad.
class _RemoteBodyPainter extends CustomPainter {
  const _RemoteBodyPainter({required this.scheme});

  final ColorScheme scheme;

  @override
  void paint(Canvas canvas, Size size) {
    final body = RRect.fromRectAndRadius(
      Rect.fromLTWH(0, 0, size.width, size.height),
      Radius.circular(size.width * 0.14),
    );
    final paint = Paint()..color = scheme.surfaceContainerLow.withValues(alpha: 0.6);
    canvas.drawRRect(body, paint);

    final border = Paint()
      ..style = PaintingStyle.stroke
      ..strokeWidth = 1.5
      ..color = scheme.outlineVariant;
    canvas.drawRRect(body, border);

    final windowWidth = size.width * 0.16;
    final window = RRect.fromRectAndRadius(
      Rect.fromCenter(
        center: Offset(size.width / 2, size.height * 0.012),
        width: windowWidth,
        height: size.height * 0.01,
      ),
      Radius.circular(windowWidth / 2),
    );
    canvas.drawRRect(window, Paint()..color = scheme.outlineVariant);
  }

  @override
  bool shouldRepaint(covariant _RemoteBodyPainter oldDelegate) => oldDelegate.scheme != scheme;
}

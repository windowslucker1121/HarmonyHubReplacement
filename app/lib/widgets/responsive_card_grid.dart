/// Lays a list of cards out in up to [maxColumns] equal-width columns,
/// collapsing toward fewer columns as the available width shrinks.
///
/// Column-major, not row-major: children are dealt round-robin into
/// [maxColumns] independent [Column]s rather than wrapped row by row. A
/// row-major layout aligns every card in the same *row* to that row's
/// tallest card, so a short card next to a tall one leaves a hole under it
/// that only closes if some later row happens to be shaped the other way
/// round -- which for a fixed page of settings cards it never does.
/// Independent columns have no such coupling: a short card costs only its
/// own height, wherever in the page it falls.
///
/// The tradeoff is reading order: children run down column 1, then down
/// column 2, rather than left-to-right across the page. That reads fine
/// for a page of self-contained cards (the common case here) and is what
/// most masonry layouts do; it would be the wrong choice for content that
/// has to be read row by row.
library;

import 'package:flutter/widgets.dart';

class ResponsiveCardGrid extends StatelessWidget {
  const ResponsiveCardGrid({
    super.key,
    required this.children,
    this.maxColumns = 2,
    this.spacing = 12,
    this.minColumnWidth = 360,
  });

  final List<Widget> children;

  /// Never lay out more columns than this, however wide the screen.
  final int maxColumns;

  final double spacing;

  /// Below this width per column, drop to fewer columns instead of
  /// squeezing cards until their content wraps awkwardly.
  final double minColumnWidth;

  Widget _stack(List<Widget> items) => Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          for (var i = 0; i < items.length; i++) ...[
            items[i],
            if (i < items.length - 1) SizedBox(height: spacing),
          ],
        ],
      );

  @override
  Widget build(BuildContext context) {
    if (children.isEmpty) return const SizedBox.shrink();

    return LayoutBuilder(
      builder: (context, constraints) {
        final columns = ((constraints.maxWidth + spacing) / (minColumnWidth + spacing))
            .floor()
            .clamp(1, maxColumns);

        if (columns == 1) return _stack(children);

        final buckets = List.generate(columns, (_) => <Widget>[]);
        for (var i = 0; i < children.length; i++) {
          buckets[i % columns].add(children[i]);
        }

        return Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            for (var c = 0; c < columns; c++) ...[
              if (c > 0) SizedBox(width: spacing),
              Expanded(child: _stack(buckets[c])),
            ],
          ],
        );
      },
    );
  }
}

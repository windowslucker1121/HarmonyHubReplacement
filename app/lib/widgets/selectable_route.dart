/// Pushing a route with selectable text.
///
/// `SelectableRegion` (what `SelectionArea` builds) needs an `Overlay`
/// ancestor to host its selection handles, and that `Overlay` lives inside
/// the `Navigator` -- so a `SelectionArea` wrapped around the app's `home`
/// (see `main.dart`) does not reach any route pushed on top of it: each
/// pushed route is its own sibling entry in that same `Overlay`, not a
/// descendant of `home`. Every route that wants selectable text needs its
/// own `SelectionArea`, which is what this helper adds.
library;

import 'package:flutter/material.dart';

/// Pushes [page] as a new full-screen route with its own [SelectionArea].
Future<T?> pushSelectable<T>(BuildContext context, Widget page) {
  return Navigator.push<T>(
    context,
    MaterialPageRoute<T>(builder: (_) => SelectionArea(child: page)),
  );
}

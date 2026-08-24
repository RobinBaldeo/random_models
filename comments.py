
def _commentary_md(build, periods, report_origin, key):
    groups = periods.get("period_groups") or [periods]
    cols = build["frame"].columns

    every = [c for g in groups for c in (g["anchor"], *g["comparison_periods"])]
    grounded = _ground_columns(every, cols, mode="contains")
    plan = _plan(groups, cols)
    if not grounded["resolved"] or not plan:
        return "No Comparison period Found", None, grounded["missing"], None

    _, subset = _subset_columns(build["frame"], grounded["resolved"], report_origin)

    tree = None
    if key is not None:
        variance = _variance_frame(build["frame"], plan, report_origin)
        try:
            normalized = apply_key(frame=variance, key=key, periods=variance.columns)
            edges = driver_edges(build["driver_df"])
        except Exception as e:
            raise MCPException(f"Commentary json failed: {e}")

        tree = {}
        for anchor, resolved in plan:
            g_cols = [c for c in resolved if c not in (NAME, anchor)]
            tree.update(nested_driver_trees(edges, normalized, periods=g_cols, anchor=anchor))
        tree = tree or None

    material_tree = {c: prune(v) for c, v in tree.items()} if tree else None
    return _to_md(subset), tree, grounded["missing"], material_tree
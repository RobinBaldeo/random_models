
def value_lookup(frame, column, grain, name=NAME, meta=META,
                 metric="metric", group="product_group"):
    flag = f"{column}{MAT_SUFFIX}"
    has_flag = flag in frame.columns
    extra = [c for c in meta if c in frame.columns]
    cols = [column] + ([flag] if has_flag else []) + extra

    def pack(r):
        v = None if pd.isna(r[column]) else r[column]
        return {"value": v,
                "material": bool(r[flag]) if has_flag and v is not None else False,
                "meta": {k: (None if pd.isna(r[k]) else r[k]) for k in extra}}

    out = {}
    primary = frame[[name, *cols]].drop_duplicates(subset=name, keep="first")
    for r in primary.to_dict("records"):
        out[str(r[name]).lstrip("_")] = pack(r)

    if metric in frame.columns and group in frame.columns:
        total = frame.loc[frame[group].astype(str).str.strip().str.casefold()
                          == str(grain).strip().casefold()]
        rows = total[[metric, *cols]].drop_duplicates(subset=metric, keep="first")
        for r in rows.to_dict("records"):
            out.setdefault(str(r[metric]).lstrip("_"), pack(r))

    return out

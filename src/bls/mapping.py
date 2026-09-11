"""Published Harmonized import categories and deterministic HTS candidates."""

import pandas as pd

from src.hts import normalize_hts_code


# Preserve the reference loader's object-string behavior with pandas 3 as well.
@pd.option_context("future.infer_string", False)
def load_bls_harmonized_series(path: str) -> dict[str, dict]:
    # Read the BLS metadata file into a pandas DataFrame.
    #
    # sep="\t":
    #   The BLS `ei.series` file is tab-separated, not comma-separated.
    #
    # dtype=str:
    #   Read every column as a string.
    #   This avoids pandas guessing numeric types for fields that are really
    #   identifiers or categorical metadata.
    df = pd.read_csv(path, sep="\t", dtype=str)

    # BLS column names may contain trailing whitespace.
    #
    # Example:
    #   "series_id    " -> "series_id"
    #
    # This makes later column access like df["series_id"] reliable.
    df.columns = [c.strip() for c in df.columns]

    # Go through every column in the DataFrame.
    for col in df.columns:

        # Only call `.str` operations on columns containing strings/objects.
        #
        # Since we loaded everything with dtype=str, most columns will satisfy
        # this condition, but it is still a defensive check.
        if df[col].dtype == "object":

            # Remove leading and trailing whitespace from every value.
            #
            # Example:
            #   "EIUIP8483    " -> "EIUIP8483"
            #
            # This matters because the raw BLS file uses fixed-width-ish
            # formatting around its tab-separated fields.
            df[col] = df[col].str.strip()

    # Filter the full BLS series metadata down to only the Harmonized
    # Import Price Index series that correspond to numeric HS codes.
    harmonized = df[

        # index_code == "IP" means Import Price Index.
        #
        # This excludes other datasets in the same `ei.series` file,
        # such as export indexes or indexes grouped by other classifications.
        (df["index_code"] == "IP")

        # Combine the first condition with the next one using logical AND.
        &

        # Keep only series IDs that match:
        #
        #   EIUIP + exactly 2 digits
        #
        # OR
        #
        #   EIUIP + exactly 4 digits
        #
        # Examples that match:
        #   EIUIP84
        #   EIUIP8483
        #
        # Examples that do not match:
        #   EIUIP
        #   EIUIP848180
        #   EIUIQ84
        #
        # `na=False` means missing series IDs should simply be treated
        # as non-matches rather than causing an error.
        df["series_id"].str.match(
            r"^EIUIP(?:\d{2}|\d{4})$",
            na=False,
        )
    ].copy()

    # At this point `harmonized` contains rows such as:
    #
    # series_id   series_name
    # EIUIP84     Machinery and mechanical appliances...
    # EIUIP8413   Pumps for liquids...
    # EIUIP8483   Parts for transmitting power...
    #
    # Now convert those rows into a dictionary for fast lookup.
    return {

        # Create the dictionary key by removing the BLS prefix "EIUIP".
        #
        # Example:
        #   "EIUIP8483" -> "8483"
        #
        # Therefore later we can directly ask:
        #
        #   if "8483" in available:
        #
        # instead of constructing the BLS series ID each time.
        row.series_id.removeprefix("EIUIP"): {

            # Preserve the complete BLS series ID because this is what
            # we eventually send to the BLS API.
            #
            # Example:
            #   "EIUIP8483"
            "series_id": row.series_id,

            # Human-readable name of the BLS category.
            #
            # Example:
            #   "Parts for transmitting power..."
            "name": row.series_name,

            # Earliest year for which this BLS series contains data.
            #
            # This is useful when deciding whether the series is usable
            # for a requested baseline date.
            "begin_year": row.begin_year,

            # Earliest period within begin_year.
            #
            # BLS monthly periods look like:
            #   M01 = January
            #   M12 = December
            #
            # Example:
            #   begin_year = "2025"
            #   begin_period = "M12"
            #
            # means the series only starts in December 2025.
            "begin_period": row.begin_period,
        }

        # Iterate over every filtered BLS Harmonized Import series.
        #
        # `itertuples()` is a convenient and relatively efficient way
        # to iterate over DataFrame rows using attribute access such as
        # `row.series_id` instead of row["series_id"].
        for row in harmonized.itertuples()
    }


def harmonized_candidates(hts_code: str, available: dict[str, dict]) -> list[dict]:
    code = normalize_hts_code(hts_code)
    return [
        {"hts_code": code, "category": category, **available[category]}
        for category in (code[:4], code[:2]) if category in available
    ]

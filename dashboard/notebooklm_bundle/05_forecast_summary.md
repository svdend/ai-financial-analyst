# PANW Revenue Forecast Summary

Three independent revenue forecasting models were run and compared.
With ~20 quarterly observations, no single model is statistically
defensible — the ensemble characterises the *range* of plausible outcomes.

## Prophet + AutoARIMA

Source: `PANW_baseline_forecasts.parquet`

| model | period_end | yhat | yhat_lower_80 | yhat_upper_80 |
| --- | --- | --- | --- | --- |
| autoarima | 2026-04-30 | 4,836,300,124.40 | 3,929,555,213.93 | 5,743,045,034.87 |
| autoarima | 2026-07-31 | 7,372,418,617.16 | 6,465,673,706.69 | 8,279,163,527.63 |
| autoarima | 2026-10-31 | 2,745,902,361.27 | 1,839,157,450.81 | 3,652,647,271.74 |
| autoarima | 2027-01-31 | 7,443,901,002.90 | 6,537,156,092.43 | 8,350,645,913.37 |
| prophet | 2026-07-31 | 4,679,287,114.97 | 3,129,549,703.72 | 6,334,347,791.24 |
| prophet | 2026-10-31 | 4,817,586,098.52 | 3,173,198,497.26 | 6,587,620,999.29 |
| prophet | 2027-01-31 | 4,955,885,082.08 | 3,259,350,877.49 | 6,731,827,426.74 |
| prophet | 2027-04-30 | 5,091,177,565.99 | 3,440,015,860.96 | 6,845,504,011.24 |

## Lasso (macro-regularized)

Source: `PANW_macro_forecast.parquet`

| model | period_end | yhat | yhat_lower_80 | yhat_upper_80 |
| --- | --- | --- | --- | --- |
| lasso | 2026-04-30 | 7,575,187,036.79 | 5,404,945,549.47 | 8,593,373,105.94 |
| lasso | 2026-07-31 | 8,159,905,295.00 | 6,775,050,419.50 | 9,739,059,079.72 |
| lasso | 2026-10-31 | 4,079,952,647.50 | 3,387,525,209.75 | 4,869,529,539.86 |
| lasso | 2027-01-31 | 6,644,226,935.28 | 5,508,218,194.35 | 8,064,363,729.84 |

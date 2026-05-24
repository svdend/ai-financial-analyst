# PANW Revenue Forecast Summary

Three independent revenue forecasting models were run and compared.
With ~20 quarterly observations, no single model is statistically
defensible — the ensemble characterises the *range* of plausible outcomes.

## Prophet + AutoARIMA

Source: `PANW_baseline_forecasts.parquet`

| model | period_end | yhat | yhat_lower_80 | yhat_upper_80 |
| --- | --- | --- | --- | --- |
| autoarima | 2026-04-01 00:00:00 | 4,836,300,124.40 | 3,929,555,213.93 | 5,743,045,034.87 |
| autoarima | 2026-07-01 00:00:00 | 7,372,418,617.16 | 6,465,673,706.69 | 8,279,163,527.63 |
| autoarima | 2026-10-01 00:00:00 | 2,745,902,361.27 | 1,839,157,450.81 | 3,652,647,271.74 |
| autoarima | 2027-01-01 00:00:00 | 7,443,901,002.90 | 6,537,156,092.43 | 8,350,645,913.37 |
| prophet | 2026-07-01 00:00:00 | 4,679,287,114.97 | 3,120,891,036.44 | 6,556,116,713.48 |
| prophet | 2026-10-01 00:00:00 | 4,817,586,098.52 | 3,155,237,685.48 | 6,491,289,926.96 |
| prophet | 2027-01-01 00:00:00 | 4,955,885,082.08 | 3,190,474,086.12 | 6,777,827,745.95 |
| prophet | 2027-04-01 00:00:00 | 5,091,177,565.99 | 3,414,982,124.53 | 6,904,190,930.43 |

## Lasso (macro-regularized)

Source: `PANW_macro_forecast.parquet`

| model | period_end | yhat | yhat_lower_80 | yhat_upper_80 |
| --- | --- | --- | --- | --- |
| lasso | 2026-04-30 00:00:00 | 7,575,186,988.95 | 5,404,945,563.59 | 8,593,373,106.86 |
| lasso | 2026-07-31 00:00:00 | 8,159,905,192.94 | 6,775,050,356.46 | 9,739,058,788.03 |
| lasso | 2026-10-31 00:00:00 | 4,079,952,596.47 | 3,387,525,178.23 | 4,869,529,394.02 |
| lasso | 2027-01-31 00:00:00 | 6,644,226,899.00 | 5,508,218,220.61 | 8,064,363,721.96 |

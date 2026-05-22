# PANW Excel Model Summary — Base / Bull / Bear

Source: `PANW_3Statement_Model.xlsx`

The model includes three scenarios (Base / Bull / Bear) with:
- Revenue growth tied to historical CAGR ± scenario multiplier
- Gross margin, operating margin, and FCF derived from historical trends
- Working capital drivers: DSO, DPO, DIO (where applicable), deferred revenue
- SBC and share buybacks split correctly per GAAP (not aggregated into capex)
- BalanceCheck = TotalAssets − (TotalLiabilities + TotalEquity) = $0 by construction

Sheets:
- **Assumptions** — scenario multipliers and key driver inputs
- **Income_Statement** — 12 historical + 16 forecast quarters
- **Balance_Sheet** — full BS with BalanceCheck row
- **Cash_Flow** — OCF / investing / financing with GAAP_OCF_residual check
- **Key_Metrics** — revenue growth %, margins, FCF yield, EV/Revenue
- **Revenue_Disaggregation** — Product vs. Subscription & Support (PANW only)
- **Sources** — per-cell provenance with accession_no and filing_url

See `dashboard/Tableau_Setup.md` for Tableau dashboard instructions.

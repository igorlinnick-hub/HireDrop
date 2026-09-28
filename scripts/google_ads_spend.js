/**
 * HireDrop — Google Ads daily spend → admin board (Ads tab).
 *
 * Install once: Google Ads → Tools → Bulk actions → Scripts → "+" → paste this
 * whole file → set the two constants below → Authorize → Preview (check the
 * log says "HTTP 200") → Save → Frequency: Daily.
 *
 * What it does: reads the last 7 days of per-ad spend (GAQL over ad_group_ad)
 * and POSTs it to the HireDrop backend, which upserts on (platform, date, ad).
 * Re-running is safe: Google revises recent days, and each run overwrites them.
 *
 * Join key: the tracking template sets utm_content={creative}, which IS
 * ad_group_ad.ad.id — the same id sent below as ad_id.
 *
 * Currency: spend is sent as-is (cost_micros / 1e6). The account must bill in
 * USD; the script refuses to post otherwise.
 */

// ---- set these two -----------------------------------------------------------
var INGEST_URL = 'https://web-production-db45.up.railway.app/api/v1/admin/ads/spend';
var INGEST_TOKEN = 'PASTE_ADS_INGEST_TOKEN_HERE'; // = ADS_INGEST_TOKEN on Railway
// -------------------------------------------------------------------------------

function main() {
  if (INGEST_TOKEN.indexOf('PASTE_') === 0) {
    throw new Error('Set INGEST_TOKEN at the top of the script first.');
  }
  var account = AdsApp.currentAccount();
  var currency = account.getCurrencyCode();
  if (currency !== 'USD') {
    throw new Error('Account bills in ' + currency + '; the board stores spend_usd.');
  }

  var query =
    'SELECT segments.date, campaign.id, campaign.name, ad_group_ad.ad.id, ' +
    'ad_group_ad.ad.name, metrics.cost_micros, metrics.impressions, metrics.clicks ' +
    'FROM ad_group_ad ' +
    'WHERE segments.date DURING LAST_7_DAYS AND metrics.impressions > 0';

  var rows = [];
  var it = AdsApp.search(query);
  while (it.hasNext()) {
    var r = it.next();
    var ad = (r.adGroupAd && r.adGroupAd.ad) || {};
    rows.push({
      date: r.segments.date,
      campaign_id: String(r.campaign.id),
      campaign_name: r.campaign.name || null,
      ad_id: String(ad.id),
      ad_name: ad.name || null,
      spend_usd: Math.round(Number(r.metrics.costMicros || 0) / 10000) / 100,
      impressions: Number(r.metrics.impressions || 0),
      clicks: Number(r.metrics.clicks || 0),
    });
  }

  var response = UrlFetchApp.fetch(INGEST_URL, {
    method: 'post',
    contentType: 'application/json',
    headers: { 'X-Ads-Ingest-Token': INGEST_TOKEN },
    payload: JSON.stringify({
      platform: 'google',
      account_id: account.getCustomerId(),
      rows: rows,
    }),
    muteHttpExceptions: true,
  });

  var code = response.getResponseCode();
  Logger.log('Posted ' + rows.length + ' rows → HTTP ' + code + ' ' + response.getContentText());
  if (code !== 200) {
    // Throwing marks the run as failed in Google Ads' script history.
    throw new Error('Ingest failed: HTTP ' + code);
  }
}

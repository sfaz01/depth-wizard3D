// NOAA solar position - mirrors src/analytics.py:sun_position so the 3D shadows and the
// server-side solar estimate agree.

const rad = (d) => (d * Math.PI) / 180;
const deg = (r) => (r * 180) / Math.PI;

function dayOfYear(date) {
  const start = Date.UTC(date.getUTCFullYear(), 0, 0);
  return Math.floor((date.getTime() - start) / 86400000);
}

// Returns { azimuth (deg clockwise from north), elevation (deg) } for a UTC Date.
export function sunPosition(lat, lon, date) {
  const doy = dayOfYear(date);
  const hour = date.getUTCHours() + date.getUTCMinutes() / 60 + date.getUTCSeconds() / 3600;
  const g = (2 * Math.PI / 365) * (doy - 1 + (hour - 12) / 24);
  const eqtime = 229.18 * (0.000075 + 0.001868 * Math.cos(g) - 0.032077 * Math.sin(g)
    - 0.014615 * Math.cos(2 * g) - 0.040849 * Math.sin(2 * g));
  const decl = 0.006918 - 0.399912 * Math.cos(g) + 0.070257 * Math.sin(g)
    - 0.006758 * Math.cos(2 * g) + 0.000907 * Math.sin(2 * g)
    - 0.002697 * Math.cos(3 * g) + 0.00148 * Math.sin(3 * g);
  const tst = hour * 60 + eqtime + 4 * lon;
  const ha = rad(tst / 4 - 180);
  const phi = rad(lat);
  let cosZen = Math.sin(phi) * Math.sin(decl) + Math.cos(phi) * Math.cos(decl) * Math.cos(ha);
  cosZen = Math.max(-1, Math.min(1, cosZen));
  const elevation = 90 - deg(Math.acos(cosZen));
  const az = deg(Math.atan2(Math.sin(ha), Math.cos(ha) * Math.sin(phi) - Math.tan(decl) * Math.cos(phi)));
  return { azimuth: (az + 180) % 360, elevation };
}

// Local clock time at the scene, using a longitude-based time zone (same rule as the server).
export function sceneDate(lon, month, hourLocal, year = new Date().getFullYear()) {
  const offset = Math.round(lon / 15);
  const h = Math.floor(hourLocal);
  const m = Math.round((hourLocal - h) * 60);
  return new Date(Date.UTC(year, month - 1, 21, h - offset, m));
}

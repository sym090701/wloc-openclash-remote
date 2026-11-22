# Third-party notices and provenance

## QPin iOS Network Location

The WLOC protobuf field interpretation and conservative response-patching
strategy in this project are derived from the MIT-licensed implementation in:

- QPin iOS Network Location, commit
  `51f4dd77ee8d9bc854dbae0c201750beccd29821`
  <https://github.com/arvinfuu/QPin-iOS-Network-Location>

QPin's required upstream attribution is retained verbatim in the repository's
top-level `NOTICE` file. Its MIT license notice follows:

> MIT License
>
> Copyright (c) 2026 QPin contributors
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

QPin identifies Yu9191's WLOC project as its upstream technical source and says
its secondary development was performed with the WLOC author's permission.
Yu9191/WLOC does not publish a license at the pinned research commit, so this
repository does not claim a separate license grant from that repository and
does not incorporate its source files directly.

## Research reference only

`acheong08/apple-corelocation-experiments` (commit
`0b136860e80585c02c12f64933995b872d7623af`) was consulted as a protocol
research reference. That repository is GPL-3.0. No source code, generated
protobuf files, or other GPL-covered material from it is incorporated here.

## Leaflet

The WebUI vendors Leaflet 1.9.4 under the BSD 2-Clause License:
<https://leafletjs.com/>. The complete license is retained at
`webui/vendor/LEAFLET-LICENSE.txt`.

## OpenStreetMap

The optional interactive map requests raster tiles from OpenStreetMap and
displays the required contributor attribution. Coordinate entry and all local
management functions continue to work when external map tiles are unavailable.

## Runtime and interoperability dependencies

The project interoperates with OpenClash and Mihomo and runs on the official
mitmproxy container image. Those projects are not redistributed in this source
repository; their own licenses and notices apply to downloaded runtime
artifacts.

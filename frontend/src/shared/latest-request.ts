/** Lets only the most recently started async request commit its result. */
export class LatestRequest {
  private version = 0;

  begin() {
    return ++this.version;
  }

  isCurrent(version: number) {
    return version === this.version;
  }
}

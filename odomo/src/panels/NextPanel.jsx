import React from 'react';
import Icon from '../Icon.jsx';
import {PanelArea} from '../ds.jsx';
import {useCopy} from '../copy.js';
import {appLinks, hostIdOf} from '../lib/applink.js';

// Remote is not in Odomo: the extra screen and taking over the desktop are
// the App's. This panel (Android, a desktop; on iPhone and iPad the bar's
// Remote is the link itself, LITE-1c) says so plainly and is the way into
// the app (TAIL-3b L8): the universal link first, and the `omodachi://` one
// small, for when the app is surely installed. Both open in a new context
// (`_blank`, no opener, no referrer) and carry nothing but the host id.
// ODOMO-2: Herdr and Agent are real panels now (HerdrPanel, AgentPanel).
export default function NextPanel({hostId}) {
  const {t} = useCopy();
  const links = appLinks('remote', hostIdOf(hostId));
  return (
    <PanelArea title={t.panels.remote} testid="panel-remote" label={t.panels.remote}>
      <div className="next" data-testid="next-remote">
        <div className="next__h">
          <Icon name="display" size={18} />
          <span className="next__t">{t.next.remoteTitle}</span>
        </div>
        <p className="next__b">{t.next.remoteBody}</p>
        <div className="next__go">
          <a className="fbtn fbtn--primary next__app" href={links.universal} target="_blank" rel="noopener noreferrer"
            data-testid="next-open-app">{t.next.openRemote}</a>
          <a className="next__scheme" href={links.scheme} target="_blank" rel="noopener noreferrer"
            data-testid="next-open-scheme">{t.next.haveApp}</a>
        </div>
      </div>
    </PanelArea>
  );
}

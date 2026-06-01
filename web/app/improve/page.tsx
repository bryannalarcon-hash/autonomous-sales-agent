// Improve-mode index (U16). The mode switch + the top-bar champion chip both navigate to /improve;
// per the handoff §81 the Improve mode's first page is the Experiment Lab, so this index permanently
// redirects there. The four real destinations live at /improve/{lab,approvals,kb,versions}.
import { redirect } from 'next/navigation';

export default function ImproveIndex() {
  redirect('/improve/lab');
}

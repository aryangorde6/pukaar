#!/usr/bin/env bash
# Sunita's circle for the demo. Re-run before filming: it also resets the response
# history to the seeded shape, so the ranking is not driven by test runs.
#
# Histories (2 pm on a weekday): Vaishali has answered every page, Ravi too from 4 km
# away, Meena - 8 m away - has never answered one. Everyone else is unknown.
set -euo pipefail
cd "$(dirname "$0")"
.venv/bin/python seed.py --subject sunita --name Sunita \
  --address "B-304, Shanti Sadan, Dadar West, Mumbai" --phone "+91 98200 00000" \
  --record "Blood group B+ · Diabetic, on metformin · Allergic to penicillin | Daughter: Priya, 98200 00001" \
  --contact "vaishali|Vaishali|neighbour, 2nd floor|aryangorde6+vaishali@gmail.com|1|40|yes" \
  --contact "anil|Anil|neighbour, building watchman's flat|aryangorde8+anil@gmail.com|1|60|yes" \
  --contact "ravi|Ravi|son|aryangorde8+ravi@gmail.com|1|4200|no" \
  --contact "meena|Meena|neighbour, same floor|aryangorde6+meena@gmail.com|2|8|no" \
  --contact "sunil|Sunil|society secretary|aryangorde6+sunil@gmail.com|2|120|yes" \
  --contact "prakash|Prakash|nephew|aryangorde8+prakash@gmail.com|2|2600|no" \
  --reset-history \
  --history "vaishali|14#weekday|5|5|210000" \
  --history "ravi|14#weekday|4|4|180000" \
  --history "meena|14#weekday|6|0|0"

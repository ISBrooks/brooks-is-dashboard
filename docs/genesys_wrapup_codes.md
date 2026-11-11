# Genesys wrap-up codes — Brooks org (snapshot 2026-07-06)

Pulled live from `GET /api/v2/routing/wrapupcodes`. 68 codes total.
The dashboard groups the sales-relevant ones via `DISPO_*` vars in `.env`;
everything else falls into an "Other" bucket.

| ID | Name |
|----|------|
| 0eef6e5f-bf7c-40b8-b5e8-9a44d6ea5a9f | Bad Transfer |
| ba385565-d393-4d46-aba5-746da4d264a5 | Billing |
| e7e80d68-2ace-4ab2-ab7b-038e5eadada6 | Call Ahead |
| 25bbc5f5-922a-4c39-b8c0-964f39839949 | Call Back |
| 7c1ae2d0-974e-4b51-9281-b19bc1a35f55 | Called, no answer. Texted in Marketsurge and emailed back |
| 4a5ff56b-85f4-4c9a-9fe2-e8a946a68cdd | Cancelled |
| 356fbb2c-370a-4869-b903-5cff12851240 | Close Conversation |
| 54824ad8-f449-4645-b07b-2b5af47689e3 | Closed |
| 0f42b1f5-ce13-4525-88b6-33b28e00c7c7 | Closed Via Email |
| 532d78b9-1985-4bb0-8020-cd89e03d4062 | Couldn't collect |
| 8651d826-7077-421e-b021-0c4f20067ea8 | Current Customer |
| c3908335-1eca-4775-a71f-07da8a7aff2d | Currently selling via email |
| 691f36c6-8f78-4847-8a46-b7cdd2700c84 | Cxl Request |
| b295bfbe-a380-445b-bdee-3f25df395cae | DNC List |
| faf7134e-887e-492f-baae-c9482945b476 | Email of a previous phone call |
| a3e2d4b0-19ce-49cf-aac3-4802832c4480 | Emailed back, awaiting response. |
| 239b6b1e-fed4-482c-ab17-107b75217773 | Follow Up |
| 81a88bbf-3d0c-4309-a04e-612afbc9a73c | General Questions |
| ee20fe69-5fdb-4d74-a06d-17906192b98e | In Contract Cancel |
| 92a0e2ef-2e0a-46d0-84d5-191a0bd1e0b8 | In Contract Save |
| 71b60974-85d2-44fb-b9be-568741c84c7c | Initial Appointment Cancel |
| 29c4a194-bc03-4283-a269-770a58177574 | Invalid call |
| 68ba682c-00ce-498a-92d3-7e421dec427f | Invalid question - it's in the script |
| 539498ab-c85f-4271-8330-d7c8ce7856e1 | LVM |
| 35ccb151-0ac8-4920-a185-a0dd88a80c93 | Moved |
| 5bab4405-038f-4dd0-80ff-797147624e9a | No Contract Cancel |
| d87acd88-15c8-4562-8c9d-f6e6532b5d3c | Online Payment |
| 3f1b8524-b703-4f1e-ba4a-d3a863120846 | Other |
| b0a8dc3a-ab60-4d55-8dc7-3d91944a5afc | Out of Contract Cancel |
| 268f5753-ba88-471b-9bc3-c42d30980288 | Out of Contract Renewal |
| 22123850-7765-4604-a60e-4de91450ef84 | Out of Contract Save |
| 5065986c-18c2-463c-97f2-053174482088 | Paid |
| a4fbc6b3-7d7a-44fe-a59e-f59b80d59917 | PHD |
| 5966f6dd-0d16-45a9-847c-6b317458b061 | Potential sale, calling in for someone. |
| 4db6ef44-a401-4cc5-8e0c-38b33a941ef4 | Referred to Baton |
| eecea163-205d-4f82-9c36-e3c73e2d5a2d | Renewal |
| 2b32a340-2578-4e29-a793-ee056954e982 | Sales Rep |
| 74939538-a987-4e94-ac12-cee8ef3de564 | Scheduling |
| d29a1b9d-8e96-4c93-8dbd-7b39b95a0a4e | Spanish |
| 500e9f3d-f060-4406-8a05-eb3b93a5047f | Spanish Cancel |
| e263e8bc-44f7-4a90-8198-b034b0d2136f | Spanish Closed |
| c43ac9c5-80d1-4d63-ab6d-61762326b367 | Spanish Follow Up |
| 0614baed-aeab-4061-b762-05972bba6653 | Spanish transferred |
| de7b24dd-b9e1-4ab9-9e44-ab5e67b53522 | Spanish Unqualified |
| aa33e9c8-80e2-485c-adcd-7c8b31756819 | Supervisor Queue |
| 82071e7b-5efb-439b-a136-2bf2696ca3e7 | Technician |
| f8d5c820-ae17-425c-9d79-f2db513845f8 | Transfer to management |
| affed174-06f6-4158-bc0f-c28bd8dfb705 | Transferred |
| 2e5e4b36-7469-4102-8f88-d1e61921beea | Transferred to Customer Service |
| 8954a768-e412-4d0b-9688-bcccd8b73eef | Transferred to IS agent |
| 2e46d49b-ceeb-41d1-953f-875927bca362 | Transferred to Loyalty |
| 9ff23314-0b71-40b7-9628-8137c6e9193f | Transferred via baton |
| b4a736f3-8cc5-44e8-91bf-553bd04db185 | Unqualified - Bed Bugs |
| 3bbdae7c-8287-46d5-b9f2-ca0e3be628e2 | Unqualified - Commercial |
| ffa725a5-10a9-47c9-8e58-3396f0344c5b | Unqualified - General |
| 0246b0f1-0114-472a-bd34-17cb76a6f4c5 | Unqualified - Honey Bees |
| 1f87c38a-bfcd-490a-8097-f8524d675dde | Unqualified - Out of Service Area |
| d764656d-3b32-4c8a-8a2f-7d055447a627 | Unqualified - Silence |
| fedbfcb6-522f-4112-be31-6e169baa2e0f | Unqualified - Termites |
| 64889d94-d529-4788-ad0a-84f40b5c89dc | Unqualified - Wildlife |
| 8b483b8e-f32e-4404-9040-28c501ee235d | Unqualified - Wrong Company |
| e7ed1dd3-ced3-49f2-b01d-6cde554211c9 | Valid call |
| 820d14d2-2831-4ab9-a8f1-2a8c351d2814 | Valid question - Add this to the Script! |
| 24979168-9de4-4f7f-8321-9ee91becf13a | Valid Question - Needed permission |
| 9aed9d4a-60d9-4ec5-8d02-f325538e485c | Welcome Call |

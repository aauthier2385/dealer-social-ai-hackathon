-- Hackathon demo data for the Dealer Social AI features (FICTIONAL people only).
--
-- Sets up Demo Motors Ford with a sales manager, three salespeople and eight customers, with
-- relationship facts, follow-ups (some overdue) and a little message history, dated from today.
-- Safe to run again: it resets these demo accounts' AI data each time.
--
-- Every demo account's password is:  DemoPass!2026
--   andrew.demo  (sales manager, store admin)   joey.marsh / jayden.price / tony.reyes (sales)
--   customers: mark.stevenson, madeline.parks, carl.jeffers, mike.carter, rosa.delgado,
--              tom.becker, dale.whitaker, priya.shah
--
-- Windows (demo database container):
--   Get-Content demo_seed\ai_followups_demo.sql | docker exec -i dealersocial-demo-postgres psql -U social -d social_app
-- Start the app once first so its tables exist.

BEGIN;

CREATE TEMP TABLE demo_people(username TEXT, first TEXT, last TEXT, role TEXT, title TEXT) ON COMMIT DROP;
INSERT INTO demo_people VALUES
 ('andrew.demo','Andrew','Authier','admin','Sales Manager'),
 ('joey.marsh','Joey','Marsh','employee','Sales'),
 ('jayden.price','Jayden','Price','employee','Sales'),
 ('tony.reyes','Tony','Reyes','employee','Sales'),
 ('mark.stevenson','Mark','Stevenson','customer',NULL),
 ('madeline.parks','Madeline','Parks','customer',NULL),
 ('carl.jeffers','Carl','Jeffers','customer',NULL),
 ('mike.carter','Mike','Carter','customer',NULL),
 ('rosa.delgado','Rosa','Delgado','customer',NULL),
 ('tom.becker','Tom','Becker','customer',NULL),
 ('dale.whitaker','Dale','Whitaker','customer',NULL),
 ('priya.shah','Priya','Shah','customer',NULL);

-- Accounts (password DemoPass!2026), store membership, staff roster, store admin.
INSERT INTO users(id,username,email,password_hash,first_name,last_name,street_address,city,state,zip_code,cell_phone,work_phone,role,status,preferred_dealer_id,privacy_consent_at)
SELECT gen_random_uuid(), d.username, d.username||'@example.com',
 '$argon2id$v=19$m=19456,t=2,p=1$HMx7DS+vWS50USvh9k4jPw$W3vkUmMzvexnKga8agJ0bkqzqwge1y4begA/9BVTMCg',
 d.first, d.last, '100 Demo Rd', 'Riverton', 'TN', '38555', '5555550100', '', d.role, 'active',
 '11111111-1111-4111-8111-111111111111', now()
FROM demo_people d ON CONFLICT (username) DO NOTHING;
UPDATE users u SET role=d.role, status='active', preferred_dealer_id='11111111-1111-4111-8111-111111111111'
FROM demo_people d WHERE u.username=d.username;
INSERT INTO dealership_memberships(dealership_id,user_id)
SELECT '11111111-1111-4111-8111-111111111111', u.id FROM users u JOIN demo_people d ON d.username=u.username
ON CONFLICT DO NOTHING;
INSERT INTO staff_profiles(id,dealership_id,user_id,display_name,title,department,active,sort_order)
SELECT gen_random_uuid(),'11111111-1111-4111-8111-111111111111',u.id,d.first||' '||d.last,d.title,
 CASE WHEN d.role='admin' THEN 'Managers' ELSE 'Sales Consultants' END, true, 300
FROM users u JOIN demo_people d ON d.username=u.username WHERE d.role<>'customer'
ON CONFLICT (dealership_id, display_name) DO UPDATE SET user_id=EXCLUDED.user_id;
INSERT INTO dealership_admins(dealership_id,user_id)
SELECT '11111111-1111-4111-8111-111111111111', id FROM users WHERE username='andrew.demo' ON CONFLICT DO NOTHING;
-- Joey Marsh has left the store: his customers need a new salesperson (handoff + at-risk demo).
UPDATE staff_profiles SET employment_inactive=true
WHERE dealership_id='11111111-1111-4111-8111-111111111111' AND display_name='Joey Marsh';
UPDATE staff_profiles SET employment_inactive=false
WHERE dealership_id='11111111-1111-4111-8111-111111111111' AND display_name IN ('Andrew Authier','Jayden Price','Tony Reyes');

-- Reset the demo customers' AI data and messages.
CREATE TEMP TABLE demo_ids ON COMMIT DROP AS
SELECT u.username, u.id FROM users u JOIN demo_people d ON d.username=u.username;
DELETE FROM follow_ups WHERE customer_id IN (SELECT id FROM demo_ids);
DELETE FROM customer_relationships WHERE customer_id IN (SELECT id FROM demo_ids);
DELETE FROM messages WHERE sender_id IN (SELECT id FROM demo_ids) OR recipient_id IN (SELECT id FROM demo_ids);

-- Relationships: who sold whom, the vehicle and the delivery date (days ago).
CREATE TEMP TABLE demo_rel(customer TEXT, salesperson TEXT, vehicle TEXT, days_ago INT, birthday TEXT) ON COMMIT DROP;
INSERT INTO demo_rel VALUES
 ('mark.stevenson','joey.marsh','26 Ford Ranger Lariat',6,NULL),
 ('rosa.delgado','joey.marsh','2025 Ford Bronco Sport',400,'04-09'),
 ('madeline.parks','jayden.price','2024 Ford Escape Active',35,'10-25'),
 ('tom.becker','jayden.price','2025 Ford Mustang',380,NULL),
 ('carl.jeffers','tony.reyes','2020 Hyundai Elantra',6,NULL),
 ('dale.whitaker','tony.reyes','2026 Ford Expedition',95,NULL),
 ('mike.carter','andrew.demo','blue F-150',345,'06-14'),
 ('priya.shah','andrew.demo','2026 Ford Maverick',20,NULL);
INSERT INTO customer_relationships(id,dealership_id,customer_id,salesperson_id,vehicle,delivery_date,birthday,updated_at)
SELECT gen_random_uuid(),'11111111-1111-4111-8111-111111111111',c.id,s.id,r.vehicle,CURRENT_DATE-r.days_ago,r.birthday,now()-(r.days_ago||' days')::interval
FROM demo_rel r JOIN demo_ids c ON c.username=r.customer JOIN demo_ids s ON s.username=r.salesperson;

-- Verified relationship facts (as the voice-note step would have saved them).
CREATE TEMP TABLE demo_fact(customer TEXT, about TEXT, category TEXT, fact TEXT, timing TEXT, certainty TEXT, quote TEXT) ON COMMIT DROP;
INSERT INTO demo_fact VALUES
 ('mark.stevenson','Mark Stevenson','vehicle','Bought a 26 Ford Ranger Lariat','','stated','he bought a 26 Ford Ranger Lariat'),
 ('mark.stevenson','Mark Stevenson','vehicle','Wanted the utility of a truck over an SUV','','stated','he wanted to have the utility of the truck over the SUV'),
 ('mark.stevenson','Bell','pets','Has a little Frenchie named Bell','','stated','he had his little Frenchie with him'),
 ('mark.stevenson','Bell','pets','Bell steals french fries at lunch','','stated','she eats my french fries when I''m eating lunch'),
 ('mark.stevenson','Mark Stevenson','hobbies','Cuts down a lot of trees','','stated','he cuts down a lot of trees'),
 ('mark.stevenson','Mark Stevenson','work','Is retired','','uncertain','he''s retired'),
 ('madeline.parks','Madeline Parks','vehicle','Loved the carbonized gray color','','stated','she really loved the color'),
 ('madeline.parks','Madeline Parks','family','Bought a farm next to her kids','','stated','she bought a farm next to all of her kids'),
 ('madeline.parks','Madeline Parks','family','Spends time with the grandkids','','stated','she can stay around with the grandkids'),
 ('madeline.parks','Madeline Parks','contact_preference','Fine with texts or calls','','uncertain','probably both'),
 ('carl.jeffers','Carl Jeffers','work','Works in pest control and travels the state','','stated','he works in pest control and travels the state'),
 ('carl.jeffers','Carl Jeffers','family','Wife recently bought a Trailblazer','September','stated','his wife bought a Trailblazer back in September'),
 ('mike.carter','Emma','life_event','Daughter Emma turns 16',to_char(CURRENT_DATE,'FMMonth'),'stated','his daughter Emma turns sixteen'),
 ('mike.carter','Mike Carter','work','Owns a sawmill','','stated','he''s on the saw all day'),
 ('mike.carter','Mike Carter','contact_preference','Prefers texts over calls','','stated','text him, don''t call'),
 ('rosa.delgado','Rosa Delgado','hobbies','Coaches her son''s baseball team','','stated','she coaches her son''s baseball team'),
 ('tom.becker','Tom Becker','hobbies','Takes the Mustang to car shows','','stated','he takes it to car shows'),
 ('dale.whitaker','Dale Whitaker','hobbies','Pulls a camper to the lake every summer','summer','stated','pulls the camper to the lake every summer'),
 ('priya.shah','Priya Shah','work','Runs a bakery downtown','','stated','she runs the bakery downtown');
INSERT INTO customer_facts(id,relationship_id,about,category,fact,timing,certainty,quote,created_by)
SELECT gen_random_uuid(), r.id, f.about, f.category, f.fact, f.timing, f.certainty, f.quote, r.salesperson_id
FROM demo_fact f JOIN demo_ids c ON c.username=f.customer
JOIN customer_relationships r ON r.customer_id=c.id AND r.dealership_id='11111111-1111-4111-8111-111111111111';

-- Follow-ups (due in N days; negative = overdue). Drafts are in each salesperson's voice.
CREATE TEMP TABLE demo_fu(customer TEXT, kind TEXT, title TEXT, due INT, channel TEXT, draft TEXT, notes TEXT, card TEXT, status TEXT) ON COMMIT DROP;
INSERT INTO demo_fu VALUES
 ('mark.stevenson','card','Thank-you card (mail within 48 hours)',-4,'mail','','','Mark, thank you so much for letting me help you into your new Ranger Lariat! Give Bell a french fry for me. Enjoy the truck! - Joey','pending'),
 ('mark.stevenson','day30','30-day call',24,'call','','["Ask how the Ranger is handling the tree work","Ask how Bell likes riding along"]','','pending'),
 ('madeline.parks','day30','30-day call',-5,'call','','["Ask how the Escape is doing on fuel","Ask how life on the farm next to the kids is going","Ask about the grandkids"]','','pending'),
 ('madeline.parks','day90','First service visit (90 days)',55,'text','Heyyy Madeline!! How is that Escape treating you?! Time for your first service visit! Book here: {booking_link} Let me know if you need anything!!','','','pending'),
 ('tom.becker','anniversary','Purchase anniversary',-15,'call','Hey Tom, just wanted to check in to see how you''re doing and how your Mustang is holding up, I can''t believe it''s been a year!! Holler at me if you need anything!','["Ask how the Mustang has been after a year","Ask about the car show season"]','','pending'),
 ('carl.jeffers','card','Thank-you card (mail within 48 hours)',-4,'mail','','','Carl, thank you for choosing Demo Motors! Hope the Elantra makes all those trips around the state easy. - Tony','done'),
 ('carl.jeffers','day30','30-day call',24,'call','','["Ask how the Elantra is doing on his work drives","Ask how his wife likes her Trailblazer"]','','pending'),
 ('dale.whitaker','day90','First service visit (90 days)',-12,'text','Hey Dale, it''s Tony at Demo Motors. Your Expedition is due for its first service visit. Grab a time here: {booking_link} Let me know if you need anything!','','','pending'),
 ('mike.carter','anniversary','Purchase anniversary',20,'call','Hey Mike, just wanted to check in to see how you''re doing and how your F-150 is holding up, I can''t believe it''s been a year!! Holler at me if you need anything!','["Ask how the F-150 is holding up at the sawmill","Ask if Emma is driving yet"]','','pending'),
 ('priya.shah','day30','30-day call',10,'call','','["Ask how the Maverick is working for bakery runs"]','','pending'),
 ('priya.shah','day1','Day-after check-in',-19,'text','Hey Priya, it''s Andrew at Demo Motors. Just checking in to see how you''re doing and if I can help you with anything about your new Ford Maverick. Let me know if you need anything!','','','sent');
INSERT INTO follow_ups(id,dealership_id,customer_id,salesperson_id,kind,title,due_date,channel,draft_text,call_notes,card_text,status,completed_at)
SELECT gen_random_uuid(),'11111111-1111-4111-8111-111111111111',c.id,r.salesperson_id,f.kind,f.title,CURRENT_DATE+f.due,f.channel,f.draft,
 COALESCE(NULLIF(f.notes,''),'[]')::jsonb,f.card,f.status,CASE WHEN f.status<>'pending' THEN now() END
FROM demo_fu f JOIN demo_ids c ON c.username=f.customer
JOIN customer_relationships r ON r.customer_id=c.id AND r.dealership_id='11111111-1111-4111-8111-111111111111';

-- Message history: last contact dates, and the texts the AI learns each salesperson's voice from.
CREATE TEMP TABLE demo_msg(sender TEXT, recipient TEXT, body TEXT, days_ago INT) ON COMMIT DROP;
INSERT INTO demo_msg VALUES
 ('andrew.demo','mike.carter','Hey Mike, it''s Andrew at Demo Motors. Just checking in to see how you''re doing and if I can help you with anything about your new Ford F-150. Let me know if you need anything!',240),
 ('mike.carter','andrew.demo','All good Andrew, truck runs great!',240),
 ('andrew.demo','priya.shah','Hey Priya, I just wanted to wish you a happy birthday 🎂 🥳 🎉 🎈!! Enjoy your day and I hope it''s a great one!',19),
 ('andrew.demo','priya.shah','Hey Priya, just wanted to check in to see how you''re doing and how your Maverick is holding up, hollar at me if you need anything!',18),
 ('joey.marsh','mark.stevenson','Yo Mark! Joey here. How''s the Ranger treating ya? Tell Bell I said hi',5),
 ('joey.marsh','rosa.delgado','Yo Rosa! Joey here w Demo Motors, how''s the Bronco treating ya?',200),
 ('jayden.price','madeline.parks','Hiii Madeline!! It''s Jayden at Demo Motors!! You were the sweetest to work with!! xoxo',34),
 ('jayden.price','tom.becker','Hey Tom!! Jayden here, hope the Mustang is turning heads!!',110),
 ('tony.reyes','carl.jeffers','Hey Carl, it''s Tony at Demo Motors. Just checking in on the Elantra. Let me know if you need anything.',5),
 ('tony.reyes','dale.whitaker','Hey Dale, it''s Tony at Demo Motors. Hope the Expedition is pulling that camper fine. Let me know if you need anything.',64);
INSERT INTO messages(id,sender_id,recipient_id,body,created_at,read_at,dealership_id)
SELECT gen_random_uuid(),s.id,r.id,m.body,now()-(m.days_ago||' days')::interval,now(),'11111111-1111-4111-8111-111111111111'
FROM demo_msg m JOIN demo_ids s ON s.username=m.sender JOIN demo_ids r ON r.username=m.recipient;

COMMIT;

SELECT 'Demo data ready: '||(SELECT count(*) FROM customer_relationships)||' relationships, '||
       (SELECT count(*) FROM follow_ups WHERE status='pending')||' pending follow-ups' AS result;
